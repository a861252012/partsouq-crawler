from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import tempfile
import zlib
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import Protocol, cast

from partsouq_crawler.db.repository import Repository
from partsouq_crawler.models.crawl import FetchResult
from partsouq_crawler.parsers.base import CatalogParser, ParseError
from partsouq_crawler.services.ingest import IngestService

NORMALIZED_TABLES = (
    "vehicle_configurations",
    "taxonomy_nodes",
    "diagrams",
    "part_numbers",
    "part_occurrences",
    "fitments",
    "compatibility_hints",
    "part_relations",
)


class ArchiveCaptureImportResultLike(Protocol):
    @property
    def response_id(self) -> int: ...

    @property
    def body_sha256(self) -> str: ...

    @property
    def created(self) -> bool: ...


class ArchiveTargetRepository(Protocol):
    async def create_or_get_run(
        self,
        run_key: str,
        seed_urls: Sequence[str],
        config: dict[str, object],
    ) -> int: ...

    async def set_run_status(
        self,
        run_id: int,
        status: str,
        *,
        blocked_reason: str | None = None,
        ended: bool = False,
    ) -> None: ...

    async def create_or_get_sqlite_import_manifest(
        self,
        *,
        source_snapshot_sha256: str,
        source_schema_version: int | None,
        source_bytes: int,
        source_counts: Mapping[str, int],
    ) -> Mapping[str, object]: ...

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
    ) -> None: ...

    async def finish_sqlite_import_manifest(
        self,
        import_id: int,
        *,
        status: str,
        target_counts: Mapping[str, int],
        error_message: str | None = None,
    ) -> None: ...

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
        source_snapshot_sha256: str | None,
        source_capture_id: int | None,
    ) -> ArchiveCaptureImportResultLike: ...

    async def add_parse_failure(
        self,
        response_id: int,
        parser_name: str,
        page_type: str,
        error: Exception,
        context: dict[str, object] | None = None,
    ) -> None: ...

    async def table_counts(self) -> dict[str, int]: ...

    async def response_body_hashes(self) -> set[str]: ...

    async def missing_provenance_count(self) -> int: ...

    async def orphan_record_count(self) -> int: ...

    async def foreign_key_violations(self) -> list[dict[str, object]]: ...


class SourceCaptureError(ValueError):
    def __init__(self, message: str, *, target_response_id: int | None = None) -> None:
        super().__init__(message)
        self.target_response_id = target_response_id


class SQLiteArchiveImportService:
    def __init__(self, repository: ArchiveTargetRepository) -> None:
        self.repository = repository

    async def run(
        self,
        *,
        sqlite_path: Path,
        run_key: str | None = None,
        batch_size: int = 100,
    ) -> dict[str, object]:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        source_path, source_exists = await asyncio.to_thread(self._resolve_file, sqlite_path)
        if not source_exists:
            raise FileNotFoundError(source_path)

        with tempfile.TemporaryDirectory(prefix="partsouq-sqlite-import-") as directory:
            snapshot_path = Path(directory) / "source.sqlite3"
            await asyncio.to_thread(self._backup_read_only, source_path, snapshot_path)
            snapshot_sha256 = await asyncio.to_thread(self._file_sha256, snapshot_path)
            source_counts = await asyncio.to_thread(self._table_counts, snapshot_path)
            source_body_hashes = await asyncio.to_thread(self._body_hashes, snapshot_path)
            source_schema_version = await asyncio.to_thread(
                self._schema_version,
                snapshot_path,
            )
            source_bytes = snapshot_path.stat().st_size
            manifest = await self.repository.create_or_get_sqlite_import_manifest(
                source_snapshot_sha256=snapshot_sha256,
                source_schema_version=source_schema_version,
                source_bytes=source_bytes,
                source_counts=source_counts,
            )
            import_id = int(str(manifest["id"]))
            resume_after_capture_id = int(str(manifest["resume_after_capture_id"]))

            effective_run_key = run_key or f"sqlite-archive-{snapshot_sha256[:16]}"
            run_id = await self.repository.create_or_get_run(
                effective_run_key,
                [],
                {
                    "source_mode": "historical_archive_sqlite_migration",
                    "source_database": source_path.name,
                    "source_snapshot_sha256": snapshot_sha256,
                    "current_or_complete": False,
                },
            )
            await self.repository.set_run_status(run_id, "running")

            selected = 0
            imported = 0
            skipped = 0
            quarantined = 0
            challenge_count = 0
            records_inserted = 0
            failures: list[dict[str, object]] = []
            last_capture_id = resume_after_capture_id
            target_counts: dict[str, int] = {}

            try:
                while True:
                    rows = await asyncio.to_thread(
                        self._capture_batch,
                        snapshot_path,
                        last_capture_id,
                        batch_size,
                    )
                    if not rows:
                        break
                    for row in rows:
                        capture_id = self._required_int(row, "capture_id")
                        source_response_id = self._required_int(row, "archive_response_id")
                        body_sha256 = self._required_str(row, "body_sha256")
                        last_capture_id = capture_id
                        selected += 1
                        if bool(row.get("is_cloudflare_challenge")):
                            challenge_count += 1
                        try:
                            inserted, created, target_response_id = await self._import_capture(
                                run_id=run_id,
                                snapshot_sha256=snapshot_sha256,
                                row=row,
                            )
                        except SourceCaptureError as error:
                            quarantined += 1
                            self._append_failure(failures, capture_id, error)
                            await self.repository.record_sqlite_import_item(
                                import_id=import_id,
                                source_capture_id=capture_id,
                                source_response_id=source_response_id,
                                body_sha256=body_sha256,
                                target_response_id=error.target_response_id,
                                status="quarantined",
                                error_message=str(error),
                            )
                            continue
                        records_inserted += inserted
                        if created:
                            imported += 1
                            item_status = "imported"
                        else:
                            skipped += 1
                            item_status = "skipped"
                        await self.repository.record_sqlite_import_item(
                            import_id=import_id,
                            source_capture_id=capture_id,
                            source_response_id=source_response_id,
                            body_sha256=body_sha256,
                            target_response_id=target_response_id,
                            status=item_status,
                            error_message=None,
                        )
                target_counts = await self.repository.table_counts()
                target_body_hashes = await self.repository.response_body_hashes()
                missing_body_hashes = sorted(source_body_hashes - target_body_hashes)
                extra_body_hashes = sorted(target_body_hashes - source_body_hashes)
                missing_provenance = await self.repository.missing_provenance_count()
                orphans = await self.repository.orphan_record_count()
                foreign_key_violations = await self.repository.foreign_key_violations()
                normalized_source_counts = self._normalized_counts(source_counts)
                normalized_target_counts = self._normalized_counts(target_counts)
                manifest_status = (
                    "completed_with_gaps"
                    if quarantined
                    or missing_body_hashes
                    or missing_provenance
                    or orphans
                    or foreign_key_violations
                    else "completed"
                )
                await self.repository.finish_sqlite_import_manifest(
                    import_id,
                    status=manifest_status,
                    target_counts=target_counts,
                )
                await self.repository.set_run_status(
                    run_id,
                    "completed_with_gaps",
                    blocked_reason=(
                        "historical_archive_migration_quarantined"
                        if quarantined
                        else "historical_archive_not_current_or_complete"
                    ),
                    ended=True,
                )
            except BaseException as error:
                await self.repository.finish_sqlite_import_manifest(
                    import_id,
                    status="failed",
                    target_counts=target_counts,
                    error_message=f"{type(error).__name__}: {error}",
                )
                await self.repository.set_run_status(
                    run_id,
                    "failed",
                    blocked_reason="sqlite_archive_migration_failed",
                    ended=True,
                )
                raise

        return {
            "run_id": run_id,
            "run_key": effective_run_key,
            "sqlite_import_id": import_id,
            "resume_after_capture_id": resume_after_capture_id,
            "source_mode": "historical_archive_sqlite_migration",
            "source_database": str(source_path),
            "source_snapshot_sha256": snapshot_sha256,
            "source_schema_version": source_schema_version,
            "source_bytes": source_bytes,
            "source_table_counts": source_counts,
            "selected": selected,
            "imported": imported,
            "skipped": skipped,
            "quarantined": quarantined,
            "challenge_count": challenge_count,
            "records_inserted": records_inserted,
            "normalized_source_counts": normalized_source_counts,
            "normalized_target_counts": normalized_target_counts,
            "body_hash_difference": {
                "missing_in_target": missing_body_hashes[:20],
                "missing_in_target_count": len(missing_body_hashes),
                "extra_in_target": extra_body_hashes[:20],
                "extra_in_target_count": len(extra_body_hashes),
                "samples_truncated": len(missing_body_hashes) > 20 or len(extra_body_hashes) > 20,
            },
            "missing_provenance": missing_provenance,
            "orphans": orphans,
            "foreign_key_violations": foreign_key_violations,
            "failures": failures,
            "current_or_complete": False,
        }

    async def _import_capture(
        self,
        *,
        run_id: int,
        snapshot_sha256: str,
        row: dict[str, object],
    ) -> tuple[int, bool, int]:
        capture_id = self._required_int(row, "capture_id")
        if row.get("response_id") is None:
            raise SourceCaptureError("archive capture has no HTTP response")
        if row.get("body_blob") is None:
            raise SourceCaptureError("HTTP response has no response body")

        expected_sha256 = self._required_str(row, "body_sha256")
        body = self._restore_body(row, expected_sha256)
        headers = self._json_string_mapping(row, "response_headers_json")
        redirects = self._json_string_sequence(row, "redirect_chain_json")
        metadata = self._json_object(row, "metadata_json")
        requested_url = self._required_str(row, "requested_url")
        final_url = self._required_str(row, "final_url")
        challenged = bool(row.get("is_cloudflare_challenge"))
        challenge_reason = self._optional_str(row.get("challenge_reason"))
        archive_source = self._required_str(row, "archive_source")
        captured_at = self._required_str(row, "captured_at")
        result = FetchResult(
            requested_url=requested_url,
            final_url=final_url,
            status=self._required_int(row, "http_status"),
            headers=headers,
            body=body,
            elapsed_ms=self._required_int(row, "elapsed_ms"),
            attempt=self._required_int(row, "attempt"),
            redirect_chain=redirects,
        )
        capture_key = self._capture_key(row, expected_sha256)
        raw_result = await self.repository.import_archive_capture_raw(
            run_id=run_id,
            result=result,
            expected_body_sha256=expected_sha256,
            challenged=challenged,
            challenge_reason=challenge_reason,
            fetched_at=self._required_str(row, "fetched_at"),
            content_type=self._optional_str(row.get("content_type")),
            charset=self._optional_str(row.get("charset")),
            archive_source=archive_source,
            collection_name=self._optional_str(row.get("collection_name")),
            captured_at=captured_at,
            warc_filename=self._optional_str(row.get("warc_filename")),
            warc_offset=self._optional_int(row.get("warc_offset")),
            warc_length=self._optional_int(row.get("warc_length")),
            archive_digest=self._optional_str(row.get("archive_digest")),
            truncation_reason=self._optional_str(row.get("truncation_reason")),
            archive_imported_at=self._required_str(row, "imported_at"),
            metadata=metadata,
            capture_key=capture_key,
            source_snapshot_sha256=snapshot_sha256,
            source_capture_id=capture_id,
        )
        if raw_result.body_sha256 != expected_sha256:
            raise RuntimeError(
                "target response body hash mismatch: "
                f"expected {expected_sha256}, got {raw_result.body_sha256}"
            )
        if challenged:
            return 0, raw_result.created, raw_result.response_id

        try:
            parsed = CatalogParser().parse(final_url, body, result.charset or "utf-8")
            inserted = await IngestService(cast(Repository, self.repository)).ingest(
                run_id=run_id,
                response_id=raw_result.response_id,
                source_url=final_url,
                parsed=parsed,
                verified_fitments=False,
                fitment_derivation=f"historical_archive_{archive_source}",
            )
        except (LookupError, OSError, ParseError, ValueError) as error:
            await self.repository.add_parse_failure(
                raw_result.response_id,
                "catalog_parser",
                "historical_archive_sqlite_migration",
                error,
                {
                    "source_snapshot_sha256": snapshot_sha256,
                    "source_capture_id": capture_id,
                },
            )
            raise SourceCaptureError(
                f"catalog parse failed: {error}",
                target_response_id=raw_result.response_id,
            ) from error
        return inserted, raw_result.created, raw_result.response_id

    @staticmethod
    def _resolve_file(path: Path) -> tuple[Path, bool]:
        resolved = path.resolve()
        return resolved, resolved.is_file()

    @staticmethod
    def _backup_read_only(source_path: Path, snapshot_path: Path) -> None:
        source_uri = f"{source_path.as_uri()}?mode=ro"
        with (
            closing(sqlite3.connect(source_uri, uri=True)) as source,
            closing(sqlite3.connect(snapshot_path)) as snapshot,
        ):
            source.backup(snapshot)

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _connect_snapshot(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    @classmethod
    def _schema_version(cls, path: Path) -> int | None:
        with closing(cls._connect_snapshot(path)) as connection:
            try:
                row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            except sqlite3.Error:
                return None
        if row is None or row[0] is None:
            return None
        return int(row[0])

    @classmethod
    def _table_counts(cls, path: Path) -> dict[str, int]:
        with closing(cls._connect_snapshot(path)) as connection:
            rows = connection.execute(
                """
                SELECT name FROM sqlite_schema
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            ).fetchall()
            counts: dict[str, int] = {}
            for row in rows:
                table = str(row["name"])
                quoted_table = table.replace('"', '""')
                result = connection.execute(
                    f'SELECT COUNT(*) FROM "{quoted_table}"'  # noqa: S608
                ).fetchone()
                counts[table] = int(result[0]) if result is not None else 0
            return counts

    @classmethod
    def _body_hashes(cls, path: Path) -> set[str]:
        with closing(cls._connect_snapshot(path)) as connection:
            return {
                str(row["sha256"])
                for row in connection.execute(
                    """
                    SELECT DISTINCT b.sha256
                    FROM archive_captures ac
                    JOIN http_responses h ON h.id = ac.response_id
                    JOIN response_bodies b ON b.sha256 = h.body_sha256
                    """
                )
            }

    @classmethod
    def _capture_batch(
        cls,
        path: Path,
        after_capture_id: int,
        batch_size: int,
    ) -> list[dict[str, object]]:
        with closing(cls._connect_snapshot(path)) as connection:
            rows = connection.execute(
                """
                SELECT
                    ac.id AS capture_id,
                    ac.response_id AS archive_response_id,
                    ac.archive_source,
                    ac.collection_name,
                    ac.captured_at,
                    ac.warc_filename,
                    ac.warc_offset,
                    ac.warc_length,
                    ac.archive_digest,
                    ac.truncation_reason,
                    ac.metadata_json,
                    ac.imported_at,
                    h.id AS response_id,
                    h.run_id AS source_run_id,
                    h.queue_id AS source_queue_id,
                    h.requested_url,
                    h.final_url,
                    h.redirect_chain_json,
                    h.http_status,
                    h.response_headers_json,
                    h.content_type,
                    h.charset,
                    h.body_sha256,
                    h.response_bytes,
                    h.elapsed_ms,
                    h.attempt,
                    h.is_cloudflare_challenge,
                    h.challenge_reason,
                    h.fetched_at,
                    b.compression,
                    b.body_blob,
                    b.original_bytes,
                    b.stored_bytes,
                    b.created_at AS body_created_at
                FROM archive_captures ac
                LEFT JOIN http_responses h ON h.id = ac.response_id
                LEFT JOIN response_bodies b ON b.sha256 = h.body_sha256
                WHERE ac.id > ?
                ORDER BY ac.id
                LIMIT ?
                """,
                (after_capture_id, batch_size),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _restore_body(row: Mapping[str, object], expected_sha256: str) -> bytes:
        raw_blob = row.get("body_blob")
        if not isinstance(raw_blob, (bytes, bytearray, memoryview)):
            raise SourceCaptureError("response body blob is not binary")
        stored = bytes(raw_blob)
        stored_bytes = SQLiteArchiveImportService._required_int(row, "stored_bytes")
        if len(stored) != stored_bytes:
            raise SourceCaptureError(
                f"stored body length mismatch: expected {stored_bytes}, got {len(stored)}"
            )
        compression = SQLiteArchiveImportService._required_str(row, "compression")
        try:
            if compression == "zlib":
                body = zlib.decompress(stored)
            elif compression == "none":
                body = stored
            else:
                raise SourceCaptureError(f"unsupported body compression: {compression}")
        except zlib.error as error:
            raise SourceCaptureError(f"invalid zlib response body: {error}") from error
        original_bytes = SQLiteArchiveImportService._required_int(row, "original_bytes")
        if len(body) != original_bytes:
            raise SourceCaptureError(
                f"original body length mismatch: expected {original_bytes}, got {len(body)}"
            )
        actual_sha256 = hashlib.sha256(body).hexdigest()
        if actual_sha256 != expected_sha256:
            raise SourceCaptureError(
                f"response body SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}"
            )
        return body

    @staticmethod
    def _capture_key(row: Mapping[str, object], body_sha256: str) -> str:
        locator = {
            "archive_source": SQLiteArchiveImportService._required_str(row, "archive_source"),
            "collection_name": SQLiteArchiveImportService._optional_str(row.get("collection_name")),
            "warc_filename": SQLiteArchiveImportService._optional_str(row.get("warc_filename")),
            "warc_offset": SQLiteArchiveImportService._optional_int(row.get("warc_offset")),
            "warc_length": SQLiteArchiveImportService._optional_int(row.get("warc_length")),
        }
        if not locator["warc_filename"]:
            locator.update(
                {
                    "captured_at": SQLiteArchiveImportService._required_str(row, "captured_at"),
                    "requested_url": SQLiteArchiveImportService._required_str(row, "requested_url"),
                    "archive_digest": SQLiteArchiveImportService._optional_str(
                        row.get("archive_digest")
                    ),
                    "body_sha256": body_sha256,
                }
            )
        serialized = json.dumps(locator, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode()).hexdigest()

    @staticmethod
    def _json_object(row: Mapping[str, object], field: str) -> dict[str, object]:
        value = SQLiteArchiveImportService._json_value(row, field)
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            raise SourceCaptureError(f"{field} must contain a JSON object")
        return cast(dict[str, object], value)

    @staticmethod
    def _json_string_mapping(row: Mapping[str, object], field: str) -> dict[str, str]:
        value = SQLiteArchiveImportService._json_value(row, field)
        if not isinstance(value, dict) or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in value.items()
        ):
            raise SourceCaptureError(f"{field} must contain a string-to-string JSON object")
        return cast(dict[str, str], value)

    @staticmethod
    def _json_string_sequence(row: Mapping[str, object], field: str) -> tuple[str, ...]:
        value = SQLiteArchiveImportService._json_value(row, field)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise SourceCaptureError(f"{field} must contain a JSON string array")
        return tuple(value)

    @staticmethod
    def _json_value(row: Mapping[str, object], field: str) -> object:
        raw = SQLiteArchiveImportService._required_str(row, field)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise SourceCaptureError(f"{field} contains invalid JSON: {error}") from error

    @staticmethod
    def _required_str(row: Mapping[str, object], field: str) -> str:
        value = row.get(field)
        if not isinstance(value, str) or not value:
            raise SourceCaptureError(f"{field} must be a non-empty string")
        return value

    @staticmethod
    def _optional_str(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise SourceCaptureError("optional string field has an invalid type")
        return value

    @staticmethod
    def _required_int(row: Mapping[str, object], field: str) -> int:
        value = row.get(field)
        if not isinstance(value, int):
            raise SourceCaptureError(f"{field} must be an integer")
        return value

    @staticmethod
    def _optional_int(value: object) -> int | None:
        if value is None:
            return None
        if not isinstance(value, int):
            raise SourceCaptureError("optional integer field has an invalid type")
        return value

    @staticmethod
    def _normalized_counts(counts: Mapping[str, int]) -> dict[str, int]:
        return {table: int(counts.get(table, 0)) for table in NORMALIZED_TABLES}

    @staticmethod
    def _append_failure(
        failures: list[dict[str, object]],
        capture_id: int,
        error: SourceCaptureError,
    ) -> None:
        if len(failures) < 50:
            failures.append(
                {
                    "source_capture_id": capture_id,
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
            )
