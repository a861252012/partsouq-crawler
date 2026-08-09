from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from partsouq_crawler.admin.db import ExecutionResult, SqlParams
from partsouq_crawler.admin.query_trace import QueryTrace
from partsouq_crawler.admin.repository import ENTITY_SPECS


@dataclass(frozen=True)
class SqlCall:
    tag: str
    sql: str
    params: SqlParams


class ScriptedDatabase:
    def __init__(
        self,
        trace: QueryTrace,
        *,
        dataset_size: int = 1,
        event_count: int = 0,
        provenance_count: int = 0,
    ) -> None:
        self.trace = trace
        self.dataset_size = dataset_size
        self.event_count = event_count
        self.provenance_count = provenance_count
        self.calls: list[SqlCall] = []
        self.closed = False

    def close(self) -> None:
        self.closed = True

    @contextmanager
    def transaction(self) -> Iterator[ScriptedDatabase]:
        yield self

    def fetch_one(
        self,
        tag: str,
        sql: str,
        params: Sequence[object] | Mapping[str, object] | None = None,
    ) -> dict[str, Any] | None:
        self._record(tag, sql, params)
        if tag.startswith("detail.base.") or tag.startswith("write.lock-base."):
            entity = tag.rsplit(".", 1)[-1]
            source_id = int(params[0]) if isinstance(params, Sequence) and params else 0
            if source_id < 1:
                return None
            return source_row(entity, source_id)
        if tag.startswith("detail.head.") or tag.startswith("write.lock-head."):
            return None
        if tag == "dashboard.source-counts":
            return {key: self.dataset_size for key in ENTITY_SPECS}
        return None

    def fetch_all(
        self,
        tag: str,
        sql: str,
        params: Sequence[object] | Mapping[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        self._record(tag, sql, params)
        if tag.startswith("list.keys."):
            assert isinstance(params, Sequence)
            requested = int(params[-1])
            count = min(self.dataset_size, requested)
            return [
                {"kind_order": 0, "sort_id": source_id}
                for source_id in range(self.dataset_size, self.dataset_size - count, -1)
            ]
        if tag.startswith("list.source-batch."):
            assert isinstance(params, Sequence)
            entity = tag.rsplit(".", 1)[-1]
            return [source_row(entity, int(value)) for value in params[1:] if int(value) > 0]
        if tag.startswith("list.manual-batch."):
            return []
        if tag.startswith("detail.events."):
            return [
                {
                    "id": index,
                    "action": "update",
                    "revision": index,
                    "base_sha256": "a" * 64,
                    "before_json": "{}",
                    "after_json": "{}",
                    "actor": "tester",
                    "reason": "test",
                    "created_at": "2026-08-10 00:00:00",
                }
                for index in range(self.event_count)
            ]
        if tag.startswith("detail.provenance."):
            return [
                {
                    "id": index,
                    "parser_name": "fixture",
                    "parser_version": "1",
                    "source_url": "https://example.test/catalog?ssd=opaque-secret",
                    "extracted_at": "2026-08-10 00:00:00",
                    "response_id": index,
                    "http_status": 200,
                    "body_sha256": "b" * 64,
                    "fetched_at": "2026-08-10 00:00:00",
                    "archive_source": None,
                    "collection_name": None,
                    "captured_at": None,
                }
                for index in range(self.provenance_count)
            ]
        if tag == "dashboard.override-counts":
            return []
        return []

    def execute(
        self,
        tag: str,
        sql: str,
        params: Sequence[object] | Mapping[str, object] | None = None,
    ) -> ExecutionResult:
        self._record(tag, sql, params)
        return ExecutionResult(lastrowid=77, rowcount=1)

    def _record(self, tag: str, sql: str, params: SqlParams) -> None:
        self.calls.append(SqlCall(tag, sql, params))
        self.trace.record(tag=tag, sql=sql, elapsed_ms=0.01, row_count=0)


def source_row(entity_type: str, source_id: int) -> dict[str, Any]:
    spec = ENTITY_SPECS[entity_type]
    row: dict[str, Any] = {"id": source_id}
    row.update({field: None for field in spec.source_fields})
    if entity_type == "part_numbers":
        row.update(
            {
                "number_raw": f"P-{source_id}",
                "number_normalized": f"P{source_id}",
                "name_en_raw": "Fixture part",
                "is_assembly_inferred": 0,
                "source_url": "https://example.test/catalog?ssd=opaque-secret",
            }
        )
    return row
