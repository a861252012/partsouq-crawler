from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import ssl
from pathlib import Path
from typing import TypedDict, cast
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp
import certifi
from warcio.archiveiterator import ArchiveIterator

from partsouq_crawler.db.repository import Repository
from partsouq_crawler.services.archive_import import ArchiveCaptureInput, ArchiveImportService
from partsouq_crawler.services.archive_queue import (
    ArchiveImportClaim,
    ArchiveImportItemInput,
    ArchiveQueueRepository,
    redact_error,
    redact_sensitive_url,
)

COLLECTION_PATTERN = re.compile(r"crawl-data/(CC-MAIN-\d{4}-\d{2})/")
ALLOWED_HOSTS = frozenset({"partsouq.com", "www.partsouq.com"})
ALLOWED_PATHS = frozenset(
    {
        "/en/catalog/genuine",
        "/en/catalog/genuine/search",
        "/en/catalog/genuine/vehicle",
        "/en/catalog/genuine/groups",
        "/en/catalog/genuine/unit",
        "/en/catalog/genuine/diagram",
        "/en/catalog/genuine/pick",
        "/en/catalog/genuine/parts",
        "/en/catalog/genuine/locate",
        "/en/search/all",
    }
)


class CommonCrawlIndexRecord(TypedDict):
    source_url: str
    warc_filename: str
    warc_offset: int
    warc_length: int
    collection_name: str
    index_timestamp: str
    index_digest: str


class ExtractedWarcResponse(TypedDict):
    source_url: str
    captured_at: str
    record_id: str | None
    payload_digest: str | None
    truncation_reason: str | None
    http_status: int
    headers: dict[str, str]
    body: bytes


class CommonCrawlImportService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        self.archive_import = ArchiveImportService(repository)

    async def run(
        self,
        *,
        run_key: str,
        index_paths: list[Path],
        max_records: int = 0,
        delay_seconds: float = 0.25,
        timeout_seconds: float = 60.0,
    ) -> dict[str, object]:
        run_id = await self.repository.create_or_get_run(
            run_key,
            [],
            {
                "source_mode": "historical_archive",
                "archive_source": "common_crawl",
                "allowed_paths": sorted(ALLOWED_PATHS),
                "current_or_complete": False,
            },
        )
        await self.repository.set_run_status(run_id, "running")
        records = self._load_records(index_paths)
        if max_records:
            records = records[:max_records]

        queue_repository = cast(ArchiveQueueRepository, self.repository)
        manifest_key = hashlib.sha256(f"common_crawl\0{run_key}".encode()).hexdigest()
        manifest_id = await queue_repository.create_or_get_archive_import_manifest(
            run_id=run_id,
            archive_source="common_crawl",
            manifest_key=manifest_key,
            metadata={
                "index_file_count": len(index_paths),
                "selected_record_count": len(records),
                "source_snapshots": [
                    {
                        "path": str(path),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "bytes": path.stat().st_size,
                    }
                    for path in index_paths
                ],
            },
        )
        item_inputs = [self._queue_item(record) for record in records]
        items_enqueued = await queue_repository.enqueue_archive_import_items(
            manifest_id,
            item_inputs,
        )
        resumed = await queue_repository.prepare_archive_import_resume(manifest_id)
        initial_counts = dict(await queue_repository.archive_import_item_counts(manifest_id))

        imported = 0
        failed = 0
        challenged = 0
        parts_parsed = 0
        records_inserted = 0
        skipped_existing_captures = 0
        failures: list[dict[str, object]] = []
        worker_id = f"common-crawl-{os.getpid()}-{uuid4().hex[:12]}"
        lease_seconds = max(60, int(timeout_seconds * 2))
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        async with aiohttp.ClientSession(
            timeout=timeout,
            auto_decompress=False,
            connector=aiohttp.TCPConnector(ssl=ssl_context),
            headers={"User-Agent": "partsouq-crawler/0.1 archive-import"},
        ) as session:
            position = 0
            while True:
                claimed_row = await queue_repository.claim_archive_import_item(
                    manifest_id,
                    worker_id=worker_id,
                    lease_seconds=lease_seconds,
                )
                if claimed_row is None:
                    break
                position += 1
                claim = ArchiveImportClaim.from_row(claimed_row)
                existing_response_id = await self.repository.response_id_for_archive_capture(
                    claim.capture_key
                )
                if existing_response_id is not None:
                    skipped_existing_captures += 1
                    await queue_repository.finish_archive_import_item(
                        claim.id,
                        "done",
                        fencing_token=claim.fencing_token,
                        response_id=existing_response_id,
                    )
                    continue
                if position > 1:
                    await asyncio.sleep(delay_seconds)
                try:
                    warc_bytes = await self._download_record(session, claim)
                    extracted = await asyncio.to_thread(self._extract_response, warc_bytes)
                    if not self._is_allowed_url(extracted["source_url"]):
                        raise ValueError("WARC target URL is outside the PartSouq allowlist")
                    report = await self.archive_import.import_bytes(
                        run_key=run_key,
                        capture=ArchiveCaptureInput(
                            source_url=extracted["source_url"],
                            archive_source="common_crawl",
                            captured_at=extracted["captured_at"],
                            run_id=run_id,
                            http_status=extracted["http_status"],
                            response_headers=extracted["headers"],
                            collection_name=claim.collection_name,
                            warc_filename=claim.warc_filename,
                            warc_offset=claim.warc_offset,
                            warc_length=claim.warc_length,
                            archive_digest=(extracted["payload_digest"] or claim.index_digest),
                            truncation_reason=extracted["truncation_reason"],
                            metadata={
                                "capture_key": claim.capture_key,
                                "warc_record_id": extracted["record_id"],
                                "index_timestamp": claim.index_timestamp,
                                "archive_import_manifest_id": manifest_id,
                                "archive_import_item_id": claim.id,
                            },
                        ),
                        body=extracted["body"],
                    )
                    response_id = cast(int, report["response_id"])
                    if report["cloudflare_challenge"]:
                        challenged += 1
                        await queue_repository.finish_archive_import_item(
                            claim.id,
                            "challenged",
                            fencing_token=claim.fencing_token,
                            response_id=response_id,
                            error=str(report["error"] or "cloudflare_challenge"),
                        )
                    elif report["error"] is not None:
                        failed += 1
                        error = redact_error(report["error"], claim.source_url)
                        await queue_repository.finish_archive_import_item(
                            claim.id,
                            "parse_failed",
                            fencing_token=claim.fencing_token,
                            response_id=response_id,
                            error=error,
                        )
                        if len(failures) < 20:
                            failures.append(
                                {
                                    "position": position,
                                    "item_id": claim.id,
                                    "source_url": redact_sensitive_url(claim.source_url),
                                    "error_type": "ParseError",
                                    "error": error,
                                }
                            )
                    else:
                        imported += 1
                        parts_parsed += cast(int, report["parts_parsed"])
                        records_inserted += cast(int, report["records_inserted"])
                        await queue_repository.finish_archive_import_item(
                            claim.id,
                            "done",
                            fencing_token=claim.fencing_token,
                            response_id=response_id,
                        )
                except (aiohttp.ClientError, OSError, RuntimeError, ValueError) as error:
                    failed += 1
                    redacted_error = redact_error(error, claim.source_url)
                    await queue_repository.finish_archive_import_item(
                        claim.id,
                        "failed",
                        fencing_token=claim.fencing_token,
                        error=redacted_error,
                    )
                    if len(failures) < 20:
                        failures.append(
                            {
                                "position": position,
                                "item_id": claim.id,
                                "source_url": redact_sensitive_url(claim.source_url),
                                "error_type": type(error).__name__,
                                "error": redacted_error,
                            }
                        )

        final_counts = dict(await queue_repository.archive_import_item_counts(manifest_id))
        await self.repository.set_run_status(
            run_id,
            "completed_with_gaps",
            blocked_reason="historical_archive_not_current_or_complete",
            ended=True,
        )
        return {
            "run_id": run_id,
            "run_key": run_key,
            "source_mode": "historical_archive",
            "archive_source": "common_crawl",
            "archive_import_manifest_id": manifest_id,
            "index_records_selected": len(records),
            "items_enqueued": items_enqueued,
            "items_resumed": resumed,
            "imported": imported,
            "skipped_existing": initial_counts.get("done", 0) + skipped_existing_captures,
            "skipped_terminal": initial_counts.get("done", 0)
            + initial_counts.get("challenged", 0)
            + initial_counts.get("http_error", 0)
            + initial_counts.get("parse_failed", 0),
            "failed": failed,
            "challenged": challenged,
            "parts_parsed": parts_parsed,
            "records_inserted": records_inserted,
            "failures": failures,
            "item_counts": final_counts,
            "current_or_complete": False,
        }

    @staticmethod
    def _load_records(index_paths: list[Path]) -> list[CommonCrawlIndexRecord]:
        records: list[CommonCrawlIndexRecord] = []
        seen_locations: set[tuple[str, int, int]] = set()
        for path in index_paths:
            with path.open(encoding="utf-8") as index_file:
                lines = enumerate(index_file, 1)
                for line_number, line in lines:
                    try:
                        raw = json.loads(line)
                        url = str(raw["url"])
                        filename = str(raw["filename"])
                        offset = int(raw["offset"])
                        length = int(raw["length"])
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                        raise ValueError(
                            f"invalid index record {path}:{line_number}: {error}"
                        ) from error
                    if not CommonCrawlImportService._is_allowed_url(url):
                        continue
                    match = COLLECTION_PATTERN.search(filename)
                    if match is None:
                        raise ValueError(f"collection missing in {path}:{line_number}")
                    location = (filename, offset, length)
                    if location in seen_locations:
                        continue
                    seen_locations.add(location)
                    records.append(
                        {
                            "source_url": url,
                            "warc_filename": filename,
                            "warc_offset": offset,
                            "warc_length": length,
                            "collection_name": match.group(1),
                            "index_timestamp": str(raw.get("timestamp") or ""),
                            "index_digest": str(raw.get("digest") or ""),
                        }
                    )
        return records

    @staticmethod
    def _is_allowed_url(url: str) -> bool:
        parsed = urlsplit(url)
        path = parsed.path.rstrip("/") or "/"
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname in ALLOWED_HOSTS
            and path in ALLOWED_PATHS
        )

    @staticmethod
    def _queue_item(record: CommonCrawlIndexRecord) -> ArchiveImportItemInput:
        location = json.dumps(
            {
                "archive_source": "common_crawl",
                "collection_name": record["collection_name"],
                "warc_filename": record["warc_filename"],
                "warc_offset": record["warc_offset"],
                "warc_length": record["warc_length"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return {
            "capture_key": hashlib.sha256(location.encode()).hexdigest(),
            **record,
        }

    @staticmethod
    async def _download_record(
        session: aiohttp.ClientSession,
        record: ArchiveImportClaim,
    ) -> bytes:
        offset = record.warc_offset
        length = record.warc_length
        url = f"https://data.commoncrawl.org/{record.warc_filename}"
        headers = {"Range": f"bytes={offset}-{offset + length - 1}"}
        async with session.get(url, headers=headers) as response:
            body = await response.read()
            if response.status not in {200, 206}:
                raise RuntimeError(f"Common Crawl returned HTTP {response.status}")
            if len(body) != length:
                raise RuntimeError(
                    f"WARC range length mismatch: expected {length}, got {len(body)}"
                )
            return body

    @staticmethod
    def _extract_response(warc_bytes: bytes) -> ExtractedWarcResponse:
        for record in ArchiveIterator(io.BytesIO(warc_bytes)):
            if record.rec_type != "response" or record.http_headers is None:
                continue
            body = record.content_stream().read()
            headers = {name: value for name, value in record.http_headers.headers}
            captured_at = record.rec_headers.get_header("WARC-Date")
            if not captured_at:
                raise ValueError("WARC response has no capture time")
            source_url = record.rec_headers.get_header("WARC-Target-URI")
            if not source_url:
                raise ValueError("WARC response has no target URL")
            return {
                "source_url": source_url,
                "captured_at": captured_at,
                "record_id": record.rec_headers.get_header("WARC-Record-ID"),
                "payload_digest": record.rec_headers.get_header("WARC-Payload-Digest"),
                "truncation_reason": record.rec_headers.get_header("WARC-Truncated"),
                "http_status": int(record.http_headers.get_statuscode()),
                "headers": headers,
                "body": body,
            }
        raise ValueError("WARC range contains no response record")
