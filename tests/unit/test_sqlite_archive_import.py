import asyncio
import hashlib
import sqlite3
import zlib
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest

from partsouq_crawler.db.repository import Repository
from partsouq_crawler.models.crawl import FetchResult
from partsouq_crawler.models.records import ParsedPage
from partsouq_crawler.services import sqlite_archive_import
from partsouq_crawler.services.archive_import import ArchiveCaptureInput, ArchiveImportService
from partsouq_crawler.services.sqlite_archive_import import SQLiteArchiveImportService

DIAGRAM_URL = "https://partsouq.com/en/catalog/genuine/diagram?c=Honda&number=31110P73A01"
DIAGRAM_HTML = b"""
<html><body>
<ul class="breadcrumb">
  <li>Genuine Parts Catalogs</li><li>Honda</li><li>INTEGRA Europe 17ST701</li>
  <li>1. ENGINE</li>
</ul>
<table>
  <tr><th>Brand</th><th>Name</th><th>Region</th><th>Npl</th><th>Manufactured</th></tr>
  <tr><td>HONDA</td><td>INTEGRA</td><td>Europe</td><td>17ST701</td><td>1998-2000</td></tr>
</table>
<div class="panel">
  <div class="unit-header"><h2>ALTERNATOR BRACKET</h2></div>
  <table>
    <tr><th>Number</th><th>Name</th><th>Code</th><th>Qty Required</th></tr>
    <tr><td>31110P73A01</td><td>BRACKET COMP.</td><td>1</td><td>1</td></tr>
  </table>
</div>
</body></html>
"""
CHALLENGE_URL = "https://partsouq.com/en/catalog/genuine/diagram?c=Toyota&number=1"
CHALLENGE_HTML = b"<html><title>Just a moment...</title></html>"


@dataclass(frozen=True, slots=True)
class FakeRawImportResult:
    response_id: int
    body_sha256: str
    created: bool


class FakeMySQLRepository:
    def __init__(self) -> None:
        self.next_run_id = 1
        self.next_response_id = 1
        self.runs: dict[str, int] = {}
        self.statuses: list[tuple[int, str, str | None, bool]] = []
        self.raw_by_key: dict[str, FakeRawImportResult] = {}
        self.raw_calls: list[dict[str, object]] = []
        self.parse_failures: list[dict[str, object]] = []
        self.ingested_response_ids: set[int] = set()
        self.normalized_counts = {table: 0 for table in sqlite_archive_import.NORMALIZED_TABLES}
        self.sqlite_imports: dict[str, dict[str, object]] = {}
        self.sqlite_import_items: dict[tuple[int, int], dict[str, object]] = {}
        self.fail_once_capture_id: int | None = None

    async def create_or_get_run(
        self,
        run_key: str,
        _seed_urls: object,
        _config: object,
    ) -> int:
        if run_key not in self.runs:
            self.runs[run_key] = self.next_run_id
            self.next_run_id += 1
        return self.runs[run_key]

    async def set_run_status(
        self,
        run_id: int,
        status: str,
        *,
        blocked_reason: str | None = None,
        ended: bool = False,
    ) -> None:
        self.statuses.append((run_id, status, blocked_reason, ended))

    async def create_or_get_sqlite_import_manifest(
        self,
        *,
        source_snapshot_sha256: str,
        source_schema_version: int | None,
        source_bytes: int,
        source_counts: object,
    ) -> dict[str, object]:
        manifest = self.sqlite_imports.get(source_snapshot_sha256)
        if manifest is None:
            manifest = {
                "id": len(self.sqlite_imports) + 1,
                "status": "running",
                "last_archive_capture_id": 0,
                "source_schema_version": source_schema_version,
                "source_bytes": source_bytes,
                "source_counts": source_counts,
            }
            self.sqlite_imports[source_snapshot_sha256] = manifest
            previous_status = "running"
            resume_after = 0
        else:
            previous_status = str(manifest["status"])
            resume_after = (
                int(str(manifest["last_archive_capture_id"]))
                if previous_status not in {"completed", "completed_with_gaps"}
                else 0
            )
            manifest["status"] = "running"
        return {
            "id": manifest["id"],
            "previous_status": previous_status,
            "resume_after_capture_id": resume_after,
        }

    async def record_sqlite_import_item(
        self,
        *,
        import_id: int,
        source_capture_id: int,
        source_response_id: int,
        body_sha256: str,
        target_response_id: int | None,
        status: str,
        error_message: str | None,
    ) -> None:
        self.sqlite_import_items[(import_id, source_capture_id)] = {
            "source_response_id": source_response_id,
            "body_sha256": body_sha256,
            "target_response_id": target_response_id,
            "status": status,
            "error_message": error_message,
        }
        manifest = next(
            item for item in self.sqlite_imports.values() if int(str(item["id"])) == import_id
        )
        manifest["last_archive_capture_id"] = max(
            int(str(manifest["last_archive_capture_id"])),
            source_capture_id,
        )

    async def finish_sqlite_import_manifest(
        self,
        import_id: int,
        *,
        status: str,
        target_counts: object,
        error_message: str | None = None,
    ) -> None:
        manifest = next(
            item for item in self.sqlite_imports.values() if int(str(item["id"])) == import_id
        )
        manifest["status"] = status
        manifest["target_counts"] = target_counts
        manifest["error_message"] = error_message

    async def import_archive_capture_raw(
        self,
        *,
        run_id: int,
        result: FetchResult,
        expected_body_sha256: str,
        challenged: bool,
        challenge_reason: str | None,
        fetched_at: str,
        content_type: str | None,
        charset: str | None,
        archive_source: str,
        collection_name: str | None,
        captured_at: str,
        warc_filename: str | None,
        warc_offset: int | None,
        warc_length: int | None,
        archive_digest: str | None,
        truncation_reason: str | None,
        archive_imported_at: str,
        metadata: dict[str, object],
        capture_key: str,
        source_snapshot_sha256: str,
        source_capture_id: int,
    ) -> FakeRawImportResult:
        if source_capture_id == self.fail_once_capture_id:
            self.fail_once_capture_id = None
            raise RuntimeError("simulated migration interruption")
        self.raw_calls.append(
            {
                "run_id": run_id,
                "result": result,
                "challenged": challenged,
                "challenge_reason": challenge_reason,
                "fetched_at": fetched_at,
                "content_type": content_type,
                "charset": charset,
                "archive_source": archive_source,
                "collection_name": collection_name,
                "captured_at": captured_at,
                "warc_filename": warc_filename,
                "warc_offset": warc_offset,
                "warc_length": warc_length,
                "archive_digest": archive_digest,
                "truncation_reason": truncation_reason,
                "archive_imported_at": archive_imported_at,
                "metadata": metadata,
                "capture_key": capture_key,
                "source_snapshot_sha256": source_snapshot_sha256,
                "source_capture_id": source_capture_id,
            }
        )
        existing = self.raw_by_key.get(capture_key)
        if existing is not None:
            return FakeRawImportResult(existing.response_id, existing.body_sha256, False)
        imported = FakeRawImportResult(self.next_response_id, expected_body_sha256, True)
        self.next_response_id += 1
        self.raw_by_key[capture_key] = imported
        return imported

    async def add_parse_failure(
        self,
        response_id: int,
        parser_name: str,
        page_type: str,
        error: Exception,
        context: dict[str, object] | None = None,
    ) -> None:
        self.parse_failures.append(
            {
                "response_id": response_id,
                "parser_name": parser_name,
                "page_type": page_type,
                "error": str(error),
                "context": context,
            }
        )

    async def table_counts(self) -> dict[str, int]:
        return {
            "http_responses": len(self.raw_by_key),
            "response_bodies": len(await self.response_body_hashes()),
            "archive_captures": len(self.raw_by_key),
            **self.normalized_counts,
        }

    async def response_body_hashes(self) -> set[str]:
        return {result.body_sha256 for result in self.raw_by_key.values()}

    async def missing_provenance_count(self) -> int:
        return 0

    async def orphan_record_count(self) -> int:
        return 0

    async def foreign_key_violations(self) -> list[dict[str, object]]:
        return []


class FakeIngestService:
    def __init__(self, repository: FakeMySQLRepository) -> None:
        self.repository = repository

    async def ingest(
        self,
        *,
        run_id: int,
        response_id: int,
        source_url: str,
        parsed: ParsedPage,
        verified_fitments: bool,
        fitment_derivation: str,
    ) -> int:
        del run_id, source_url
        assert verified_fitments is False
        assert fitment_derivation.startswith("historical_archive_")
        if response_id in self.repository.ingested_response_ids:
            return 0
        self.repository.ingested_response_ids.add(response_id)
        created = 0
        if parsed.vehicle is not None:
            self.repository.normalized_counts["vehicle_configurations"] += 1
            created += 1
        taxonomy_count = sum(len(taxonomy.path) for taxonomy in parsed.taxonomies)
        self.repository.normalized_counts["taxonomy_nodes"] += taxonomy_count
        created += taxonomy_count
        self.repository.normalized_counts["diagrams"] += len(parsed.diagrams)
        created += len(parsed.diagrams)
        part_numbers = {part.number_raw for part in parsed.parts}
        self.repository.normalized_counts["part_numbers"] += len(part_numbers)
        created += len(part_numbers)
        if parsed.vehicle is not None and parsed.diagrams:
            self.repository.normalized_counts["part_occurrences"] += len(parsed.parts)
            self.repository.normalized_counts["fitments"] += len(parsed.parts)
            created += len(parsed.parts) * 2
        self.repository.normalized_counts["compatibility_hints"] += len(parsed.compatibility_hints)
        self.repository.normalized_counts["part_relations"] += len(parsed.part_relations)
        created += len(parsed.compatibility_hints) + len(parsed.part_relations)
        return created


def _create_source(path: Path) -> None:
    async def scenario() -> None:
        repository = await Repository.create(path)
        service = ArchiveImportService(repository)
        await service.import_bytes(
            run_key="legacy-archive",
            capture=ArchiveCaptureInput(
                source_url=DIAGRAM_URL,
                archive_source="common_crawl",
                collection_name="CC-MAIN-2021-49",
                captured_at="2021-07-24T01:23:19Z",
                warc_filename="crawl-data/test-1.warc.gz",
                warc_offset=100,
                warc_length=200,
                archive_digest="sha1:catalog",
                metadata={"warc_record_id": "urn:uuid:catalog", "nested": {"value": 1}},
            ),
            body=DIAGRAM_HTML,
        )
        await service.import_bytes(
            run_key="legacy-archive",
            capture=ArchiveCaptureInput(
                source_url=CHALLENGE_URL,
                archive_source="common_crawl",
                collection_name="CC-MAIN-2021-49",
                captured_at="2021-07-24T02:00:00Z",
                http_status=403,
                response_headers={"Server": "cloudflare", "cf-mitigated": "challenge"},
                warc_filename="crawl-data/test-2.warc.gz",
                warc_offset=300,
                warc_length=400,
                archive_digest="sha1:challenge",
                truncation_reason="length",
                metadata={"warc_record_id": "urn:uuid:challenge"},
            ),
            body=CHALLENGE_HTML,
        )
        await repository.close()

    asyncio.run(scenario())


def _create_parse_failure_source(path: Path) -> None:
    async def scenario() -> None:
        repository = await Repository.create(path)
        await ArchiveImportService(repository).import_bytes(
            run_key="legacy-parse-failure",
            capture=ArchiveCaptureInput(
                source_url="https://partsouq.com/en/catalog/genuine/parts",
                archive_source="wayback",
                captured_at="2020-01-02T03:04:05Z",
                archive_digest="sha1:unparseable",
                metadata={"wayback_timestamp": "20200102030405"},
            ),
            body=b"<html><h1>Parts</h1></html>",
        )
        await repository.close()

    asyncio.run(scenario())


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_sqlite_archive_migration_is_read_only_raw_first_and_idempotent(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "legacy.sqlite3"
    _create_source(source_path)
    source_sha256_before = _sha256(source_path)
    target = FakeMySQLRepository()
    monkeypatch.setattr(sqlite_archive_import, "IngestService", FakeIngestService)

    async def scenario() -> tuple[dict[str, object], dict[str, object]]:
        service = SQLiteArchiveImportService(target)
        first = await service.run(sqlite_path=source_path, batch_size=1)
        second = await service.run(sqlite_path=source_path, batch_size=1)
        return first, second

    first, second = asyncio.run(scenario())

    assert _sha256(source_path) == source_sha256_before
    assert first["selected"] == 2
    assert first["imported"] == 2
    assert first["skipped"] == 0
    assert first["quarantined"] == 0
    assert first["challenge_count"] == 1
    assert first["body_hash_difference"] == {
        "missing_in_target": [],
        "missing_in_target_count": 0,
        "extra_in_target": [],
        "extra_in_target_count": 0,
        "samples_truncated": False,
    }
    assert first["missing_provenance"] == 0
    assert first["orphans"] == 0
    assert first["foreign_key_violations"] == []
    assert first["normalized_source_counts"] == first["normalized_target_counts"]
    assert len(str(first["source_snapshot_sha256"])) == 64
    assert second["selected"] == 2
    assert second["imported"] == 0
    assert second["skipped"] == 2
    assert second["quarantined"] == 0
    assert second["normalized_target_counts"] == first["normalized_target_counts"]

    first_pass_calls = target.raw_calls[:2]
    assert [call["source_capture_id"] for call in first_pass_calls] == [1, 2]
    assert first_pass_calls[0]["metadata"] == {
        "nested": {"value": 1},
        "warc_record_id": "urn:uuid:catalog",
    }
    assert first_pass_calls[0]["content_type"] == "text/html"
    assert first_pass_calls[0]["charset"] == "utf-8"
    assert first_pass_calls[0]["archive_imported_at"]
    assert first_pass_calls[1]["challenged"] is True
    assert first_pass_calls[1]["challenge_reason"] == "cloudflare_challenge"
    assert first_pass_calls[1]["truncation_reason"] == "length"
    assert first_pass_calls[1]["metadata"] == {"warc_record_id": "urn:uuid:challenge"}
    assert first_pass_calls[0]["capture_key"] == target.raw_calls[2]["capture_key"]
    assert first_pass_calls[1]["capture_key"] == target.raw_calls[3]["capture_key"]
    assert len(target.ingested_response_ids) == 1
    assert len(target.sqlite_import_items) == 2
    assert next(iter(target.sqlite_imports.values()))["status"] == "completed"


def test_interrupted_migration_resumes_after_persisted_capture_cursor(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "resume.sqlite3"
    _create_source(source_path)
    target = FakeMySQLRepository()
    target.fail_once_capture_id = 2
    monkeypatch.setattr(sqlite_archive_import, "IngestService", FakeIngestService)
    service = SQLiteArchiveImportService(target)

    with pytest.raises(RuntimeError, match="simulated migration interruption"):
        asyncio.run(service.run(sqlite_path=source_path, batch_size=1))

    manifest = next(iter(target.sqlite_imports.values()))
    assert manifest["status"] == "failed"
    assert manifest["last_archive_capture_id"] == 1

    resumed = asyncio.run(service.run(sqlite_path=source_path, batch_size=1))

    assert resumed["resume_after_capture_id"] == 1
    assert resumed["selected"] == 1
    assert resumed["imported"] == 1
    assert len(target.sqlite_import_items) == 2
    assert manifest["status"] == "completed"


def test_corrupt_source_body_is_quarantined_without_target_write(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "corrupt.sqlite3"
    _create_source(source_path)
    corrupted = zlib.compress(b"tampered body")
    with closing(sqlite3.connect(source_path)) as connection:
        connection.execute(
            """
            UPDATE response_bodies
            SET body_blob = ?, original_bytes = ?, stored_bytes = ?
            WHERE sha256 = (
                SELECT body_sha256 FROM http_responses
                WHERE is_cloudflare_challenge = 0 LIMIT 1
            )
            """,
            (corrupted, len(b"tampered body"), len(corrupted)),
        )
        connection.commit()
    target = FakeMySQLRepository()
    monkeypatch.setattr(sqlite_archive_import, "IngestService", FakeIngestService)

    report = asyncio.run(
        SQLiteArchiveImportService(target).run(sqlite_path=source_path, batch_size=1)
    )

    assert report["selected"] == 2
    assert report["imported"] == 1
    assert report["quarantined"] == 1
    assert report["challenge_count"] == 1
    assert len(report["body_hash_difference"]["missing_in_target"]) == 1
    assert report["body_hash_difference"]["missing_in_target_count"] == 1
    assert report["body_hash_difference"]["extra_in_target"] == []
    assert report["failures"][0]["source_capture_id"] == 1
    assert "SHA-256 mismatch" in report["failures"][0]["error"]
    assert len(target.raw_calls) == 1
    assert target.raw_calls[0]["challenged"] is True
    assert target.ingested_response_ids == set()


def test_parse_failure_is_stored_raw_before_quarantine(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "parse-failure.sqlite3"
    _create_parse_failure_source(source_path)
    target = FakeMySQLRepository()
    monkeypatch.setattr(sqlite_archive_import, "IngestService", FakeIngestService)

    report = asyncio.run(SQLiteArchiveImportService(target).run(sqlite_path=source_path))

    assert report["selected"] == 1
    assert report["imported"] == 0
    assert report["skipped"] == 0
    assert report["quarantined"] == 1
    assert report["body_hash_difference"] == {
        "missing_in_target": [],
        "missing_in_target_count": 0,
        "extra_in_target": [],
        "extra_in_target_count": 0,
        "samples_truncated": False,
    }
    assert len(target.raw_by_key) == 1
    assert len(target.parse_failures) == 1
    assert target.parse_failures[0]["page_type"] == "historical_archive_sqlite_migration"
    assert target.parse_failures[0]["context"]["source_capture_id"] == 1
