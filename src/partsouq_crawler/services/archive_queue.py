from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, TypedDict
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

VIN_PATTERN = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$", re.IGNORECASE)
REDACTED = "[REDACTED]"


class ArchiveImportItemInput(TypedDict):
    capture_key: str
    source_url: str
    collection_name: str
    warc_filename: str
    warc_offset: int
    warc_length: int
    index_timestamp: str
    index_digest: str


@dataclass(frozen=True, slots=True)
class ArchiveImportClaim:
    id: int
    fencing_token: int
    capture_key: str
    source_url: str
    collection_name: str
    warc_filename: str
    warc_offset: int
    warc_length: int
    index_timestamp: str
    index_digest: str

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> ArchiveImportClaim:
        return cls(
            id=int(str(row["id"])),
            fencing_token=int(str(row["fencing_token"])),
            capture_key=str(row["capture_key"]),
            source_url=str(row["source_url"]),
            collection_name=str(row["collection_name"]),
            warc_filename=str(row["warc_filename"]),
            warc_offset=int(str(row["warc_offset"])),
            warc_length=int(str(row["warc_length"])),
            index_timestamp=str(row.get("index_timestamp") or ""),
            index_digest=str(row.get("index_digest") or ""),
        )


class ArchiveQueueRepository(Protocol):
    async def create_or_get_archive_import_manifest(
        self,
        *,
        run_id: int,
        archive_source: str,
        manifest_key: str,
        metadata: Mapping[str, object],
    ) -> int: ...

    async def enqueue_archive_import_items(
        self,
        manifest_id: int,
        items: Sequence[ArchiveImportItemInput],
    ) -> int: ...

    async def prepare_archive_import_resume(self, manifest_id: int) -> int: ...

    async def claim_archive_import_item(
        self,
        manifest_id: int,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> Mapping[str, object] | None: ...

    async def finish_archive_import_item(
        self,
        item_id: int,
        status: str,
        *,
        fencing_token: int,
        response_id: int | None = None,
        error: str | None = None,
    ) -> None: ...

    async def archive_import_item_counts(self, manifest_id: int) -> Mapping[str, int]: ...


def redact_sensitive_url(url: str) -> str:
    """Return a report-safe URL while leaving the stored source URL untouched."""
    parts = urlsplit(url)
    redacted_path = "/".join(
        quote(REDACTED, safe="[]") if VIN_PATTERN.fullmatch(unquote(segment)) else segment
        for segment in parts.path.split("/")
    )
    redacted_query: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        sensitive = key.casefold() in {"ssd", "vin"} or VIN_PATTERN.fullmatch(value)
        redacted_query.append((key, REDACTED if sensitive else value))
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            redacted_path,
            urlencode(redacted_query, doseq=True, safe="[]"),
            parts.fragment,
        )
    )


def redact_error(error: object, source_url: str) -> str:
    message = str(error) or type(error).__name__
    if source_url:
        message = message.replace(source_url, redact_sensitive_url(source_url))
    return message
