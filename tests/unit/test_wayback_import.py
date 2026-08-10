import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from partsouq_crawler.crawl.challenge import detect_challenge
from partsouq_crawler.services.archive_import import ArchiveCaptureInput
from partsouq_crawler.services.wayback_import import (
    WaybackDownload,
    WaybackImportService,
)

HEADER = ["timestamp", "original", "statuscode", "digest", "length"]
DIAGRAM_URL = (
    "https://partsouq.com/en/catalog/genuine/diagram?c=Honda&number=31110P73A01&ssd=PRIVATE-SSD"
)
SECOND_URL = (
    "https://partsouq.com/en/catalog/genuine/diagram?c=Toyota&number=90915YZZF2&ssd=SECOND-SSD"
)


class FakeArchiveQueueRepository:
    def __init__(self) -> None:
        self.run_id = 1
        self.run_status: list[str] = []
        self.manifests: dict[tuple[int, str, str], dict[str, object]] = {}
        self.items: list[dict[str, Any]] = []

    async def create_or_get_run(
        self,
        run_key: str,
        seed_urls: Sequence[str],
        config: dict[str, object],
    ) -> int:
        assert run_key
        assert seed_urls == []
        assert config["archive_source"] == "wayback"
        return self.run_id

    async def set_run_status(
        self,
        run_id: int,
        status: str,
        *,
        blocked_reason: str | None = None,
        ended: bool = False,
    ) -> None:
        assert run_id == self.run_id
        self.run_status.append(status)

    async def create_or_get_archive_import_manifest(
        self,
        *,
        run_id: int,
        archive_source: str,
        manifest_key: str,
        metadata: Mapping[str, object],
    ) -> int:
        key = (run_id, archive_source, manifest_key)
        manifest = self.manifests.get(key)
        if manifest is None:
            manifest = {
                "id": len(self.manifests) + 1,
                "metadata": metadata,
            }
            self.manifests[key] = manifest
        return int(manifest["id"])

    async def prepare_archive_import_resume(self, manifest_id: int) -> int:
        resumed = 0
        for item in self.items:
            if item["manifest_id"] == manifest_id and item["status"] in {
                "in_progress",
                "failed",
            }:
                item["status"] = "pending"
                item["worker_id"] = None
                resumed += 1
        return resumed

    async def enqueue_archive_import_items(
        self,
        manifest_id: int,
        records: Sequence[Mapping[str, object]],
    ) -> int:
        inserted = 0
        existing = {
            str(item["capture_key"]) for item in self.items if item["manifest_id"] == manifest_id
        }
        for record in records:
            capture_key = str(record["capture_key"])
            if capture_key in existing:
                continue
            self.items.append(
                {
                    "id": len(self.items) + 1,
                    "manifest_id": manifest_id,
                    **record,
                    "status": "pending",
                    "attempts": 0,
                    "fencing_token": 0,
                    "worker_id": None,
                    "response_id": None,
                }
            )
            existing.add(capture_key)
            inserted += 1
        return inserted

    async def claim_archive_import_item(
        self,
        manifest_id: int,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> Mapping[str, Any] | None:
        assert lease_seconds >= 60
        for item in self.items:
            if item["manifest_id"] == manifest_id and item["status"] == "pending":
                item["status"] = "in_progress"
                item["attempts"] += 1
                item["fencing_token"] += 1
                item["worker_id"] = worker_id
                return dict(item)
        return None

    async def finish_archive_import_item(
        self,
        item_id: int,
        status: str,
        *,
        fencing_token: int,
        error: str | None = None,
        response_id: int | None = None,
    ) -> None:
        item = next(item for item in self.items if item["id"] == item_id)
        assert item["fencing_token"] == fencing_token
        item.update(status=status, error=error, response_id=response_id, worker_id=None)

    async def archive_import_item_counts(self, manifest_id: int) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.items:
            if item["manifest_id"] == manifest_id:
                status = str(item["status"])
                counts[status] = counts.get(status, 0) + 1
        return counts


class FakeArchiveImporter:
    def __init__(self) -> None:
        self.raw_captures: list[tuple[ArchiveCaptureInput, bytes]] = []

    async def import_bytes(
        self,
        *,
        run_key: str,
        capture: ArchiveCaptureInput,
        body: bytes,
    ) -> dict[str, object]:
        assert run_key
        self.raw_captures.append((capture, body))
        decision = detect_challenge(capture.http_status, capture.response_headers, body)
        parse_error = "catalog parse failed" if body == b"parse-error" else None
        return {
            "response_id": len(self.raw_captures),
            "cloudflare_challenge": decision.challenged,
            "error": decision.reason or parse_error,
        }


def _write_cdx(path: Path, rows: list[list[str]]) -> None:
    path.write_text(json.dumps([HEADER, *rows]), encoding="utf-8")


def _row(
    url: str,
    *,
    timestamp: str = "20210724012319",
    digest: str = "TEST-DIGEST",
) -> list[str]:
    return [timestamp, url, "200", digest, "1234"]


def test_zero_selected_records_excludes_vin_and_non_catalog_routes(tmp_path: Path) -> None:
    async def scenario() -> None:
        index_path = tmp_path / "empty.json"
        _write_cdx(
            index_path,
            [
                _row("https://partsouq.com/en/catalog/genuine/vin?ssd=secret"),
                _row(
                    "https://partsouq.com/en/catalog/genuine/diagram"
                    "?q=JH4DA9350LS000000&ssd=secret",
                    digest="VIN",
                ),
                _row("https://partsouq.com/en/catalog-description-731.html", digest="DESC"),
                _row("https://example.com/en/catalog/genuine/diagram", digest="HOST"),
            ],
        )
        repository = FakeArchiveQueueRepository()
        importer = FakeArchiveImporter()

        async def unexpected_download(*_args: object) -> WaybackDownload:
            raise AssertionError("zero-record import must not download")

        service = WaybackImportService(repository, archive_import=importer)
        service._download_capture = unexpected_download  # type: ignore[method-assign]
        report = await service.run(run_key="wayback-empty", index_paths=[index_path])

        assert report["index_records_selected"] == 0
        assert report["queued"] == 0
        assert report["downloaded"] == 0
        assert report["queue"] == {}
        assert importer.raw_captures == []

    asyncio.run(scenario())


def test_duplicate_capture_is_queued_once_and_done_is_not_downloaded_again(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        first_path = tmp_path / "page-1.json"
        second_path = tmp_path / "page-2.json"
        _write_cdx(first_path, [_row(DIAGRAM_URL), _row(DIAGRAM_URL)])
        _write_cdx(second_path, [_row(DIAGRAM_URL)])
        repository = FakeArchiveQueueRepository()
        importer = FakeArchiveImporter()
        downloads: list[str] = []

        async def download(_session: object, playback_url: str) -> WaybackDownload:
            downloads.append(playback_url)
            return WaybackDownload(
                status=200,
                headers={"Content-Type": "text/html"},
                body=b"<html><body>catalog</body></html>",
                final_url=playback_url,
            )

        service = WaybackImportService(repository, archive_import=importer)
        service._download_capture = download  # type: ignore[method-assign]
        first = await service.run(
            run_key="wayback-duplicates",
            index_paths=[first_path, second_path],
            delay_seconds=0,
        )
        second = await service.run(
            run_key="wayback-duplicates",
            index_paths=[first_path, second_path],
            delay_seconds=0,
        )

        assert first["index_records_selected"] == 1
        assert first["queued"] == 1
        assert first["queue"] == {"done": 1}
        assert second["queued"] == 0
        assert second["downloaded"] == 0
        assert len(downloads) == 1
        assert repository.items[0]["source_url"] == DIAGRAM_URL
        assert "PRIVATE-SSD" not in json.dumps(first)
        assert "PRIVATE-SSD" not in json.dumps(second)

    asyncio.run(scenario())


def test_interrupted_item_is_resumed_without_redownloading_done_item(tmp_path: Path) -> None:
    async def scenario() -> None:
        index_path = tmp_path / "resume.json"
        _write_cdx(
            index_path,
            [
                _row(DIAGRAM_URL, digest="FIRST"),
                _row(SECOND_URL, timestamp="20210724012320", digest="SECOND"),
            ],
        )
        repository = FakeArchiveQueueRepository()
        importer = FakeArchiveImporter()
        attempts: dict[str, int] = {}
        interrupt_second = True

        async def download(_session: object, playback_url: str) -> WaybackDownload:
            nonlocal interrupt_second
            attempts[playback_url] = attempts.get(playback_url, 0) + 1
            if "SECOND-SSD" in playback_url and interrupt_second:
                interrupt_second = False
                raise asyncio.CancelledError
            return WaybackDownload(
                status=200,
                headers={"Content-Type": "text/html"},
                body=b"<html><body>catalog</body></html>",
                final_url=playback_url,
            )

        service = WaybackImportService(repository, archive_import=importer)
        service._download_capture = download  # type: ignore[method-assign]
        with pytest.raises(asyncio.CancelledError):
            await service.run(
                run_key="wayback-resume",
                index_paths=[index_path],
                delay_seconds=0,
            )

        report = await service.run(
            run_key="wayback-resume",
            index_paths=[index_path],
            delay_seconds=0,
        )

        first_playback = WaybackImportService._playback_url("20210724012319", DIAGRAM_URL)
        second_playback = WaybackImportService._playback_url("20210724012320", SECOND_URL)
        assert attempts[first_playback] == 1
        assert attempts[second_playback] == 2
        assert report["queued"] == 0
        assert report["queue"] == {"done": 2}
        assert len(importer.raw_captures) == 2

    asyncio.run(scenario())


def test_challenge_is_terminal_and_preserves_raw_capture_without_report_secret(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        index_path = tmp_path / "challenge.json"
        _write_cdx(index_path, [_row(DIAGRAM_URL)])
        repository = FakeArchiveQueueRepository()
        importer = FakeArchiveImporter()
        challenge_body = b"<html><title>Just a moment...</title></html>"

        async def download(_session: object, playback_url: str) -> WaybackDownload:
            return WaybackDownload(
                status=403,
                headers={"server": "cloudflare", "cf-mitigated": "challenge"},
                body=challenge_body,
                final_url=playback_url,
            )

        service = WaybackImportService(repository, archive_import=importer)
        service._download_capture = download  # type: ignore[method-assign]
        first = await service.run(
            run_key="wayback-challenge",
            index_paths=[index_path],
            delay_seconds=0,
        )
        second = await service.run(
            run_key="wayback-challenge",
            index_paths=[index_path],
            delay_seconds=0,
        )

        assert first["queue"] == {"challenged": 1}
        assert first["failures"][0]["status"] == "challenged"  # type: ignore[index]
        assert second["downloaded"] == 0
        assert importer.raw_captures[0][1] == challenge_body
        capture = importer.raw_captures[0][0]
        assert capture.source_url == DIAGRAM_URL
        assert capture.captured_at == "2021-07-24T01:23:19Z"
        assert capture.metadata["capture_key"] == repository.items[0]["capture_key"]
        assert "PRIVATE-SSD" in str(capture.metadata["playback_url"])
        manifest = next(iter(repository.manifests.values()))
        snapshots = manifest["metadata"]["source_snapshots"]  # type: ignore[index]
        assert snapshots[0]["sha256"]  # type: ignore[index]
        assert repository.items[0]["response_id"] == 1
        assert "PRIVATE-SSD" not in json.dumps(first)

    asyncio.run(scenario())


def test_http_and_parse_failures_are_terminal_after_raw_capture(tmp_path: Path) -> None:
    async def scenario() -> None:
        index_path = tmp_path / "terminal.json"
        _write_cdx(
            index_path,
            [
                _row(DIAGRAM_URL, digest="HTTP"),
                _row(SECOND_URL, timestamp="20210724012320", digest="PARSE"),
            ],
        )
        repository = FakeArchiveQueueRepository()
        importer = FakeArchiveImporter()
        downloads = 0

        async def download(_session: object, playback_url: str) -> WaybackDownload:
            nonlocal downloads
            downloads += 1
            if "PRIVATE-SSD" in playback_url:
                return WaybackDownload(404, {"Content-Type": "text/html"}, b"missing", playback_url)
            return WaybackDownload(
                200,
                {"Content-Type": "text/html"},
                b"parse-error",
                playback_url,
            )

        service = WaybackImportService(repository, archive_import=importer)
        service._download_capture = download  # type: ignore[method-assign]
        first = await service.run(
            run_key="wayback-terminal",
            index_paths=[index_path],
            delay_seconds=0,
        )
        second = await service.run(
            run_key="wayback-terminal",
            index_paths=[index_path],
            delay_seconds=0,
        )

        assert first["queue"] == {"http_error": 1, "parse_failed": 1}
        assert second["downloaded"] == 0
        assert downloads == 2
        assert len(importer.raw_captures) == 2

    asyncio.run(scenario())
