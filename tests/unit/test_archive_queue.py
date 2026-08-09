import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

from partsouq_crawler.services.archive_import import ArchiveCaptureInput
from partsouq_crawler.services.archive_queue import (
    ArchiveImportClaim,
    ArchiveImportItemInput,
    redact_error,
    redact_sensitive_url,
)
from partsouq_crawler.services.common_crawl_import import (
    ALLOWED_PATHS,
    CommonCrawlImportService,
    ExtractedWarcResponse,
)

WARC_FILENAME = "crawl-data/CC-MAIN-2021-49/segments/test/warc/test.warc.gz"
VIN = "JH4KA9650MC012345"


class FakeArchiveRepository:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.items: list[dict[str, object]] = []
        self._next_item_id = 1

    async def response_id_for_archive_capture(self, _capture_key: str) -> int | None:
        return None

    async def create_or_get_run(
        self,
        _run_key: str,
        _seed_urls: Sequence[str],
        _config: dict[str, object],
    ) -> int:
        return 11

    async def set_run_status(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def create_or_get_archive_import_manifest(
        self,
        *,
        run_id: int,
        archive_source: str,
        manifest_key: str,
        metadata: Mapping[str, object],
    ) -> int:
        assert run_id == 11
        assert archive_source == "common_crawl"
        assert len(manifest_key) == 64
        assert metadata["selected_record_count"] == 3
        snapshots = cast(list[dict[str, object]], metadata["source_snapshots"])
        assert len(snapshots) == 1
        assert len(str(snapshots[0]["sha256"])) == 64
        assert int(str(snapshots[0]["bytes"])) > 0
        self.events.append("manifest")
        return 23

    async def enqueue_archive_import_items(
        self,
        manifest_id: int,
        items: Sequence[ArchiveImportItemInput],
    ) -> int:
        assert manifest_id == 23
        existing = {str(item["capture_key"]) for item in self.items}
        inserted = 0
        for item in items:
            if item["capture_key"] in existing:
                continue
            self.items.append(
                {
                    "id": self._next_item_id,
                    **item,
                    "status": "pending",
                    "response_id": None,
                    "error": None,
                    "fencing_token": 0,
                }
            )
            self._next_item_id += 1
            inserted += 1
        self.events.append("enqueue")
        return inserted

    async def prepare_archive_import_resume(self, manifest_id: int) -> int:
        assert manifest_id == 23
        resumed = 0
        for item in self.items:
            if item["status"] == "failed":
                item["status"] = "pending"
                resumed += 1
        return resumed

    async def claim_archive_import_item(
        self,
        manifest_id: int,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> Mapping[str, object] | None:
        assert manifest_id == 23
        assert worker_id.startswith("common-crawl-")
        assert lease_seconds >= 60
        for item in self.items:
            if item["status"] == "pending":
                item["status"] = "in_progress"
                item["fencing_token"] = int(str(item["fencing_token"])) + 1
                return item
        return None

    async def finish_archive_import_item(
        self,
        item_id: int,
        status: str,
        *,
        fencing_token: int,
        response_id: int | None = None,
        error: str | None = None,
    ) -> None:
        item = next(item for item in self.items if item["id"] == item_id)
        assert item["fencing_token"] == fencing_token
        item["status"] = status
        item["response_id"] = response_id
        item["error"] = error

    async def archive_import_item_counts(self, manifest_id: int) -> Mapping[str, int]:
        assert manifest_id == 23
        counts: dict[str, int] = {}
        for item in self.items:
            status = str(item["status"])
            counts[status] = counts.get(status, 0) + 1
        return counts


class FakeArchiveImport:
    def __init__(self) -> None:
        self.response_id = 100

    async def import_bytes(self, **kwargs: object) -> dict[str, object]:
        capture = cast(ArchiveCaptureInput, kwargs["capture"])
        source_url = capture.source_url
        self.response_id += 1
        challenged = parse_qs(urlsplit(source_url).query).get("case") == ["challenge"]
        return {
            "response_id": self.response_id,
            "cloudflare_challenge": challenged,
            "error": "cloudflare_challenge" if challenged else None,
            "parts_parsed": 1,
            "records_inserted": 2,
        }


def _write_index(path: Path, urls: Sequence[str]) -> None:
    records = []
    for position, url in enumerate(urls, 1):
        records.append(
            json.dumps(
                {
                    "url": url,
                    "filename": WARC_FILENAME,
                    "offset": position * 100,
                    "length": 50,
                    "timestamp": "20210724012319",
                    "digest": f"sha1:{position}",
                }
            )
        )
    path.write_text("\n".join(records) + "\n", encoding="utf-8")


def test_index_allows_catalog_routes_and_preserves_sensitive_source_url(tmp_path: Path) -> None:
    sensitive_url = f"https://partsouq.com/en/catalog/genuine/vehicle?ssd=secret&vin={VIN}&q={VIN}"
    urls = [
        *(f"https://partsouq.com{path}?case={index}" for index, path in enumerate(ALLOWED_PATHS)),
        sensitive_url,
        "https://partsouq.com/en/catalog/genuine/diagram-extra?ssd=secret",
        "https://example.com/en/catalog/genuine/diagram?ssd=secret",
    ]
    index_path = tmp_path / "index.ndjson"
    _write_index(index_path, urls)

    records = CommonCrawlImportService._load_records([index_path])

    assert len(records) == len(ALLOWED_PATHS) + 1
    assert records[-1]["source_url"] == sensitive_url
    assert records[-1]["warc_offset"] == (len(ALLOWED_PATHS) + 1) * 100


def test_persistent_queue_resumes_failures_and_keeps_terminal_items(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        urls = [
            "https://partsouq.com/en/catalog/genuine/diagram?case=done",
            "https://partsouq.com/en/catalog/genuine/vehicle?case=retry&ssd=private",
            f"https://partsouq.com/en/search/all?case=challenge&q={VIN}",
        ]
        index_path = tmp_path / "index.ndjson"
        _write_index(index_path, urls)
        repository = FakeArchiveRepository()
        service = CommonCrawlImportService(cast(object, repository))
        service.archive_import = cast(object, FakeArchiveImport())
        attempts: dict[str, int] = {}

        async def fake_download(_session: object, claim: ArchiveImportClaim) -> bytes:
            source_url = claim.source_url
            assert repository.events[:2] == ["manifest", "enqueue"]
            attempts[source_url] = attempts.get(source_url, 0) + 1
            if "case=retry" in source_url and attempts[source_url] == 1:
                raise OSError(f"temporary failure for {source_url}")
            return source_url.encode()

        def fake_extract(body: bytes) -> ExtractedWarcResponse:
            return {
                "source_url": body.decode(),
                "captured_at": "2021-07-24T01:23:19Z",
                "record_id": "urn:uuid:test",
                "payload_digest": "sha1:test",
                "truncation_reason": None,
                "http_status": 200,
                "headers": {"Content-Type": "text/html"},
                "body": b"<html></html>",
            }

        monkeypatch.setattr(
            CommonCrawlImportService,
            "_download_record",
            staticmethod(fake_download),
        )
        monkeypatch.setattr(
            CommonCrawlImportService,
            "_extract_response",
            staticmethod(fake_extract),
        )

        first = await service.run(
            run_key="queue-test",
            index_paths=[index_path],
            delay_seconds=0,
        )
        second = await service.run(
            run_key="queue-test",
            index_paths=[index_path],
            delay_seconds=0,
        )
        third = await service.run(
            run_key="queue-test",
            index_paths=[index_path],
            delay_seconds=0,
        )

        assert first["items_enqueued"] == 3
        assert first["imported"] == 1
        assert first["failed"] == 1
        assert first["challenged"] == 1
        failure = cast(list[dict[str, object]], first["failures"])[0]
        assert "private" not in str(failure)
        assert VIN not in str(failure)
        assert "ssd=[REDACTED]" in str(failure)

        assert second["items_enqueued"] == 0
        assert second["items_resumed"] == 1
        assert second["imported"] == 1
        assert second["failed"] == 0
        assert third["imported"] == 0
        assert third["challenged"] == 0
        assert cast(dict[str, int], third["item_counts"]) == {"done": 2, "challenged": 1}
        assert attempts[urls[0]] == 1
        assert attempts[urls[1]] == 2
        assert attempts[urls[2]] == 1
        assert repository.items[1]["source_url"] == urls[1]

    asyncio.run(scenario())


def test_sensitive_url_redaction_covers_query_and_path_vin() -> None:
    url = f"https://partsouq.com/en/catalog/genuine/vehicle/{VIN}?ssd=a%2Bb&vin={VIN}&q={VIN}"

    redacted = redact_sensitive_url(url)

    assert VIN not in redacted
    assert "a%2Bb" not in redacted
    assert redacted.count("[REDACTED]") == 4


def test_empty_exception_message_keeps_error_type() -> None:
    assert redact_error(TimeoutError(), "") == "TimeoutError"
