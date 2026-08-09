from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from partsouq_crawler.models.records import ParsedPage, PartRecord
from partsouq_crawler.services.ingest import IngestService


class _Cursor:
    def __init__(self, rows: Sequence[dict[str, object]] = (), *, rowcount: int = 0) -> None:
        self._rows = list(rows)
        self.rowcount = rowcount

    async def fetchall(self) -> list[dict[str, object]]:
        return self._rows


class _CountingConnection:
    def __init__(self) -> None:
        self.execute_calls = 0
        self.executemany_calls = 0
        self.part_rows: dict[tuple[object, str], dict[str, object]] = {}
        self.sources: set[tuple[object, ...]] = set()
        self.records_extracted = 0

    @property
    def round_trips(self) -> int:
        return self.execute_calls + self.executemany_calls

    async def execute(self, sql: str, parameters: Sequence[object] = ()) -> _Cursor:
        self.execute_calls += 1
        normalized_sql = " ".join(sql.split())
        if normalized_sql.startswith("SELECT id, part_brand_raw, number_raw"):
            keys = {
                (parameters[index], str(parameters[index + 1]))
                for index in range(0, len(parameters), 2)
            }
            rows = [row for key, row in self.part_rows.items() if key in keys]
            return _Cursor(sorted(rows, key=lambda row: int(row["id"])))
        if normalized_sql.startswith("UPDATE crawl_runs"):
            self.records_extracted += int(parameters[0])
            return _Cursor(rowcount=1)
        raise AssertionError(f"unexpected execute: {normalized_sql}")

    async def executemany(
        self,
        sql: str,
        rows: Sequence[Sequence[object]],
    ) -> _Cursor:
        self.executemany_calls += 1
        normalized_sql = " ".join(sql.split())
        if normalized_sql.startswith("INSERT IGNORE INTO part_numbers"):
            inserted = 0
            for row in rows:
                key = (row[0], str(row[1]))
                if key in self.part_rows:
                    continue
                inserted += 1
                self.part_rows[key] = {
                    "id": len(self.part_rows) + 1,
                    "part_brand_raw": row[0],
                    "number_raw": row[1],
                }
            return _Cursor(rowcount=inserted)
        if normalized_sql.startswith("INSERT IGNORE INTO record_sources"):
            inserted = 0
            for row in rows:
                key = tuple(row[:5])
                if key not in self.sources:
                    inserted += 1
                    self.sources.add(key)
            return _Cursor(rowcount=inserted)
        raise AssertionError(f"unexpected executemany: {normalized_sql}")


class _MySQLRepository:
    backend_name = "mysql"

    def __init__(self) -> None:
        self.connection = _CountingConnection()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[_CountingConnection]:
        yield self.connection


def _ingest_parts(count: int) -> tuple[int, int]:
    async def scenario() -> tuple[int, int]:
        repository = _MySQLRepository()
        parsed = ParsedPage(
            page_type="part_list",
            parts=[PartRecord(number_raw=f"{number:08d}") for number in range(count)],
        )
        inserted = await IngestService(repository).ingest(  # type: ignore[arg-type]
            run_id=1,
            response_id=1,
            source_url="https://partsouq.com/en/catalog/genuine/parts",
            parsed=parsed,
        )
        assert len(repository.connection.part_rows) == count
        return inserted, repository.connection.round_trips

    return asyncio.run(scenario())


def test_mysql_part_ingest_round_trips_do_not_grow_per_row() -> None:
    one_inserted, one_round_trips = _ingest_parts(1)
    bulk_inserted, bulk_round_trips = _ingest_parts(500)

    assert one_inserted == 1
    assert bulk_inserted == 500
    assert bulk_round_trips == one_round_trips


def test_mysql_part_ingest_preserves_null_and_leading_zero_identity() -> None:
    async def scenario() -> None:
        repository = _MySQLRepository()
        parsed = ParsedPage(
            page_type="part_list",
            parts=[
                PartRecord(number_raw="00123"),
                PartRecord(number_raw="123"),
                PartRecord(number_raw="00000"),
            ],
        )
        service = IngestService(repository)  # type: ignore[arg-type]
        first = await service.ingest(
            run_id=1,
            response_id=1,
            source_url="https://partsouq.com/en/catalog/genuine/parts",
            parsed=parsed,
        )
        second = await service.ingest(
            run_id=1,
            response_id=1,
            source_url="https://partsouq.com/en/catalog/genuine/parts",
            parsed=parsed,
        )

        assert first == 3
        assert second == 0
        assert set(repository.connection.part_rows) == {
            (None, "00123"),
            (None, "123"),
            (None, "00000"),
        }
        assert len(repository.connection.sources) == 3

    asyncio.run(scenario())
