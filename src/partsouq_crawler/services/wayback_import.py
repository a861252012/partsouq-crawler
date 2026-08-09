from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import ssl
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TypedDict, cast
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import aiohttp
import certifi

from partsouq_crawler.db.repository import Repository
from partsouq_crawler.services.archive_import import ArchiveCaptureInput, ArchiveImportService
from partsouq_crawler.services.archive_queue import (
    ArchiveImportClaim,
    ArchiveImportItemInput,
    ArchiveQueueRepository,
    redact_error,
    redact_sensitive_url,
)

ARCHIVE_SOURCE = "wayback"
WAYBACK_BASE_URL = "https://web.archive.org/web"
VIN_PATTERN = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$", re.IGNORECASE)
TIMESTAMP_PATTERN = re.compile(r"^\d{14}$")


class WaybackCdxRecord(TypedDict):
    capture_key: str
    playback_timestamp: str
    original_url: str
    archive_digest: str
    http_status: int | None
    content_length: int | None
    source_path: str
    source_row: int
    source_snapshot_sha256: str
    metadata: dict[str, object]


class ArchiveCaptureImporter(Protocol):
    async def import_bytes(
        self,
        *,
        run_key: str,
        capture: ArchiveCaptureInput,
        body: bytes,
    ) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class WaybackDownload:
    status: int
    headers: dict[str, str]
    body: bytes
    final_url: str


class WaybackImportService:
    def __init__(
        self,
        repository: Repository,
        *,
        archive_import: ArchiveCaptureImporter | None = None,
    ) -> None:
        self.repository = repository
        self.queue_repository = cast(ArchiveQueueRepository, repository)
        self.archive_import = archive_import or ArchiveImportService(repository)

    async def run(
        self,
        *,
        run_key: str,
        index_paths: list[Path],
        worker_id: str | None = None,
        max_records: int = 0,
        delay_seconds: float = 2.0,
        timeout_seconds: float = 60.0,
    ) -> dict[str, object]:
        if max_records < 0:
            raise ValueError("max_records cannot be negative")
        if delay_seconds < 0:
            raise ValueError("delay_seconds cannot be negative")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        records = self._load_records(index_paths)
        if max_records:
            records = records[:max_records]
        manifest_key = self._manifest_key(records)
        run_id = await self.repository.create_or_get_run(
            run_key,
            [],
            {
                "source_mode": "historical_archive",
                "archive_source": ARCHIVE_SOURCE,
                "allowlist": "/en/catalog and /en/catalog/**",
                "current_or_complete": False,
            },
        )
        await self.repository.set_run_status(run_id, "running")
        manifest_id = await self.queue_repository.create_or_get_archive_import_manifest(
            run_id=run_id,
            archive_source=ARCHIVE_SOURCE,
            manifest_key=manifest_key,
            metadata={
                "index_file_count": len(index_paths),
                "selected_record_count": len(records),
                "source_snapshots": self._source_snapshots(index_paths),
            },
        )
        queued = await self.queue_repository.enqueue_archive_import_items(
            manifest_id,
            [self._queue_item(record) for record in records],
        )
        resumed = await self.queue_repository.prepare_archive_import_resume(manifest_id)
        initial_counts = dict(await self.queue_repository.archive_import_item_counts(manifest_id))

        downloaded = 0
        imported = 0
        failures: list[dict[str, object]] = []
        active_worker_id = worker_id or f"wayback-{os.getpid()}-{uuid4().hex[:12]}"
        lease_seconds = max(60, int(timeout_seconds * 2))
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        async with aiohttp.ClientSession(
            timeout=timeout,
            connector=aiohttp.TCPConnector(ssl=ssl_context),
            headers={"User-Agent": "partsouq-crawler/0.1 wayback-archive-import"},
        ) as session:
            position = 0
            while True:
                claimed_row = await self.queue_repository.claim_archive_import_item(
                    manifest_id,
                    worker_id=active_worker_id,
                    lease_seconds=lease_seconds,
                )
                if claimed_row is None:
                    break
                position += 1
                claim = ArchiveImportClaim.from_row(claimed_row)
                if position > 1:
                    await asyncio.sleep(delay_seconds)
                try:
                    self._validate_claim(claim)
                    capture_key = self._capture_key(
                        claim.index_timestamp,
                        claim.source_url,
                        claim.index_digest,
                    )
                    playback_url = self._playback_url(
                        claim.index_timestamp,
                        claim.source_url,
                    )
                    response = await self._download_capture(session, playback_url)
                    downloaded += 1
                    report = await self.archive_import.import_bytes(
                        run_key=run_key,
                        capture=ArchiveCaptureInput(
                            source_url=claim.source_url,
                            archive_source=ARCHIVE_SOURCE,
                            captured_at=self._captured_at_iso(claim.index_timestamp),
                            run_id=run_id,
                            http_status=response.status,
                            response_headers=response.headers,
                            collection_name=claim.collection_name,
                            archive_digest=claim.index_digest,
                            metadata={
                                "capture_key": capture_key,
                                "playback_timestamp": claim.index_timestamp,
                                "cdx_length": claim.warc_length or None,
                                "archive_import_manifest_id": manifest_id,
                                "archive_import_item_id": claim.id,
                                "playback_url": playback_url,
                                "playback_final_url": response.final_url,
                            },
                        ),
                        body=response.body,
                    )
                    status = self._terminal_status(response.status, report)
                    response_id = int(cast(str | int, report["response_id"]))
                    error = cast(str | None, report.get("error"))
                    safe_error = redact_error(error, claim.source_url) if error else None
                    await self.queue_repository.finish_archive_import_item(
                        claim.id,
                        status,
                        fencing_token=claim.fencing_token,
                        error=safe_error,
                        response_id=response_id,
                    )
                    if status == "done":
                        imported += 1
                    elif len(failures) < 20:
                        failures.append(
                            {
                                "position": position,
                                "item_id": claim.id,
                                "capture_key": capture_key,
                                "source_url": redact_sensitive_url(claim.source_url),
                                "status": status,
                                "error": safe_error,
                            }
                        )
                except asyncio.CancelledError:
                    raise
                except (aiohttp.ClientError, OSError, RuntimeError, ValueError) as error:
                    safe_error = redact_error(error, claim.source_url)
                    await self.queue_repository.finish_archive_import_item(
                        claim.id,
                        "failed",
                        fencing_token=claim.fencing_token,
                        error=safe_error,
                    )
                    if len(failures) < 20:
                        failures.append(
                            {
                                "position": position,
                                "item_id": claim.id,
                                "capture_key": self._capture_key(
                                    claim.index_timestamp,
                                    claim.source_url,
                                    claim.index_digest,
                                ),
                                "source_url": redact_sensitive_url(claim.source_url),
                                "status": "failed",
                                "error_type": type(error).__name__,
                                "error": safe_error,
                            }
                        )

        counts = dict(await self.queue_repository.archive_import_item_counts(manifest_id))
        await self.repository.set_run_status(
            run_id,
            "completed_with_gaps",
            blocked_reason="historical_archive_not_current_or_complete",
            ended=True,
        )
        return {
            "run_id": run_id,
            "run_key": run_key,
            "manifest_id": manifest_id,
            "manifest_key": manifest_key,
            "source_mode": "historical_archive",
            "archive_source": ARCHIVE_SOURCE,
            "index_files": len(index_paths),
            "index_records_selected": len(records),
            "queued": queued,
            "resumed": resumed,
            "downloaded": downloaded,
            "imported": imported,
            "skipped_existing": initial_counts.get("done", 0),
            "skipped_terminal": initial_counts.get("done", 0)
            + initial_counts.get("challenged", 0)
            + initial_counts.get("http_error", 0)
            + initial_counts.get("parse_failed", 0),
            "queue": counts,
            "failures": failures,
            "current_or_complete": False,
        }

    @classmethod
    def _load_records(cls, index_paths: list[Path]) -> list[WaybackCdxRecord]:
        records: list[WaybackCdxRecord] = []
        seen: set[str] = set()
        for path in index_paths:
            raw_bytes = path.read_bytes()
            snapshot_sha256 = hashlib.sha256(raw_bytes).hexdigest()
            try:
                page = json.loads(raw_bytes)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid CDX JSON in {path.name}: {error.msg}") from error
            if not isinstance(page, list) or not page:
                raise ValueError(f"CDX page {path.name} must contain a header row")
            header = page[0]
            if not isinstance(header, list) or not all(isinstance(value, str) for value in header):
                raise ValueError(f"CDX page {path.name} has an invalid header row")
            if len(set(header)) != len(header):
                raise ValueError(f"CDX page {path.name} has duplicate header fields")
            required = {"timestamp", "original", "digest"}
            missing = required - set(header)
            if missing:
                raise ValueError(
                    f"CDX page {path.name} is missing fields: {', '.join(sorted(missing))}"
                )

            for row_number, raw_row in enumerate(page[1:], start=2):
                if not isinstance(raw_row, list) or len(raw_row) != len(header):
                    raise ValueError(f"CDX page {path.name} row {row_number} is malformed")
                row = dict(zip(header, raw_row, strict=True))
                playback_timestamp = str(row["timestamp"])
                original_url = str(row["original"])
                archive_digest = str(row["digest"])
                if not TIMESTAMP_PATTERN.fullmatch(playback_timestamp):
                    raise ValueError(
                        f"CDX page {path.name} row {row_number} has an invalid timestamp"
                    )
                if not archive_digest:
                    raise ValueError(f"CDX page {path.name} row {row_number} has an empty digest")
                if not cls._is_allowed_url(original_url):
                    continue
                capture_key = cls._capture_key(
                    playback_timestamp,
                    original_url,
                    archive_digest,
                )
                if capture_key in seen:
                    continue
                seen.add(capture_key)
                status = cls._optional_int(row.get("statuscode"), "statuscode", path, row_number)
                length = cls._optional_int(row.get("length"), "length", path, row_number)
                records.append(
                    {
                        "capture_key": capture_key,
                        "playback_timestamp": playback_timestamp,
                        "original_url": original_url,
                        "archive_digest": archive_digest,
                        "http_status": status,
                        "content_length": length,
                        "source_path": str(path),
                        "source_row": row_number,
                        "source_snapshot_sha256": snapshot_sha256,
                        "metadata": {
                            "cdx_statuscode": status,
                            "cdx_length": length,
                        },
                    }
                )
        return records

    @staticmethod
    def _queue_item(record: WaybackCdxRecord) -> ArchiveImportItemInput:
        return {
            "capture_key": record["capture_key"],
            "source_url": record["original_url"],
            "collection_name": ARCHIVE_SOURCE,
            "warc_filename": "",
            "warc_offset": 0,
            "warc_length": record["content_length"] or 0,
            "index_timestamp": record["playback_timestamp"],
            "index_digest": record["archive_digest"],
        }

    @classmethod
    def _validate_claim(cls, claim: ArchiveImportClaim) -> None:
        if not TIMESTAMP_PATTERN.fullmatch(claim.index_timestamp):
            raise ValueError("archive queue item has an invalid Wayback timestamp")
        if not claim.index_digest:
            raise ValueError("archive queue item has an empty Wayback digest")
        if not cls._is_allowed_url(claim.source_url):
            raise ValueError("archive queue item is outside the Wayback allowlist")

    @staticmethod
    def _captured_at_iso(playback_timestamp: str) -> str:
        captured_at = datetime.strptime(playback_timestamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        return captured_at.isoformat().replace("+00:00", "Z")

    @staticmethod
    def _source_snapshots(index_paths: Sequence[Path]) -> list[dict[str, object]]:
        return [
            {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size,
            }
            for path in index_paths
        ]

    @staticmethod
    def _optional_int(
        value: object,
        field: str,
        path: Path,
        row_number: int,
    ) -> int | None:
        if value in (None, "", "-"):
            return None
        try:
            result = int(cast(str | int, value))
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"CDX page {path.name} row {row_number} has an invalid {field}"
            ) from error
        return result

    @staticmethod
    def _is_allowed_url(url: str) -> bool:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"}:
            return False
        if parsed.username or parsed.password:
            return False
        if (parsed.hostname or "").lower() not in {"partsouq.com", "www.partsouq.com"}:
            return False
        path = parsed.path.rstrip("/") or "/"
        if path != "/en/catalog" and not path.startswith("/en/catalog/"):
            return False
        if "vin" in {segment.lower() for segment in path.split("/") if segment}:
            return False
        query = parse_qs(parsed.query, keep_blank_values=True)
        return not any(
            VIN_PATTERN.fullmatch(value)
            for key, values in query.items()
            if key.lower() == "q"
            for value in values
        )

    @staticmethod
    def _capture_key(
        playback_timestamp: str,
        original_url: str,
        archive_digest: str,
    ) -> str:
        identity = "\0".join((playback_timestamp, original_url, archive_digest))
        return hashlib.sha256(identity.encode()).hexdigest()

    @staticmethod
    def _manifest_key(records: Sequence[WaybackCdxRecord]) -> str:
        canonical = "\n".join(sorted(record["capture_key"] for record in records))
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _playback_url(playback_timestamp: str, original_url: str) -> str:
        return f"{WAYBACK_BASE_URL}/{playback_timestamp}id_/{original_url}"

    @staticmethod
    async def _download_capture(
        session: aiohttp.ClientSession,
        playback_url: str,
    ) -> WaybackDownload:
        async with session.get(playback_url) as response:
            return WaybackDownload(
                status=response.status,
                headers={name: value for name, value in response.headers.items()},
                body=await response.read(),
                final_url=str(response.url),
            )

    @staticmethod
    def _terminal_status(http_status: int, report: Mapping[str, object]) -> str:
        if bool(report.get("cloudflare_challenge")):
            return "challenged"
        if http_status >= 400:
            return "http_error"
        if report.get("error"):
            return "parse_failed"
        return "done"
