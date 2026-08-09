from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import aiosqlite

from partsouq_crawler.db.repository import utc_now

PROVENANCE_BATCH_SIZE = 500


async def add_source(
    connection: aiosqlite.Connection,
    *,
    record_type: str,
    record_id: int,
    response_id: int,
    parser_name: str,
    parser_version: str,
    source_url: str,
) -> None:
    await connection.execute(
        """
        INSERT OR IGNORE INTO record_sources(
            record_type, record_id, response_id, parser_name,
            parser_version, source_url, extracted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record_type,
            record_id,
            response_id,
            parser_name,
            parser_version,
            source_url,
            utc_now(),
        ),
    )


async def add_sources(
    connection: Any,
    *,
    sources: Sequence[tuple[str, int, int, str, str, str]],
) -> None:
    """Insert a bounded batch of MySQL provenance rows."""
    if not sources:
        return
    extracted_at = utc_now()
    for start in range(0, len(sources), PROVENANCE_BATCH_SIZE):
        await connection.executemany(
            """
            INSERT IGNORE INTO record_sources(
                record_type, record_id, response_id, parser_name,
                parser_version, source_url, extracted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [(*source, extracted_at) for source in sources[start : start + PROVENANCE_BATCH_SIZE]],
        )
