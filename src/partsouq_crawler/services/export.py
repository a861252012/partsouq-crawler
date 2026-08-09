from __future__ import annotations

import csv
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from partsouq_crawler.db.repository import Repository
from partsouq_crawler.exporters.csv_exporter import spreadsheet_safe
from partsouq_crawler.services.archive_queue import redact_sensitive_url

EXPORT_BATCH_SIZE = 1_000

EXPORT_COLUMNS = (
    "Brand",
    "Name",
    "Model",
    "Prod period",
    "Production from",
    "Production to",
    "產品英文名稱",
    "Number",
    "零件大分類",
    "零件中分類",
    "零件小分類",
    "Category path",
    "Range",
    "Part range from",
    "Part range to",
    "Diagram range",
    "Condition",
    "Description",
    "Options",
    "Diagram code",
    "Diagram name",
    "Callout",
    "Quantity",
    "Note",
    "Source URL",
    "Source mode",
    "Archive source",
    "Captured at",
    "Archive truncation",
    "Response ID",
    "Response SHA-256",
    "Confidence",
    "Derivation",
    "Verified fitment",
)


class ExportService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    async def rows(
        self,
        *,
        include_compatibility_hints: bool = False,
        include_unverified_fitments: bool = False,
        include_sensitive_source_urls: bool = False,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        async for batch in self._batches(
            include_compatibility_hints=include_compatibility_hints,
            include_unverified_fitments=include_unverified_fitments,
            include_sensitive_source_urls=include_sensitive_source_urls,
        ):
            rows.extend(batch)
        return rows

    async def _batches(
        self,
        *,
        include_compatibility_hints: bool,
        include_unverified_fitments: bool,
        include_sensitive_source_urls: bool,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        fitment_id = 0
        source_id = 0
        while True:
            cursor = await self.repository.connection.execute(
                """
            SELECT
                f.id AS fitment_id,
                rs.id AS source_record_id,
                v.catalog_brand AS brand,
                v.name_raw AS vehicle_name,
                v.model_raw AS model,
                v.prod_period_raw AS prod_period,
                v.production_from,
                v.production_to,
                pn.name_en_raw AS part_name,
                pn.number_raw AS part_number,
                tn.path_raw AS category_path,
                po.part_range_raw,
                po.part_from,
                po.part_to,
                d.diagram_range_raw,
                po.part_condition_raw,
                v.description_raw,
                v.options_raw,
                d.diagram_code_raw,
                d.diagram_name_raw,
                po.callout_raw,
                po.quantity_raw,
                po.note_raw,
                f.source_url,
                CASE WHEN ac.id IS NULL THEN 'live_http' ELSE 'historical_archive' END
                  AS source_mode,
                ac.archive_source,
                ac.captured_at,
                ac.truncation_reason,
                rs.response_id,
                hr.body_sha256,
                f.confidence,
                f.derivation,
                f.is_verified
            FROM fitments f
            JOIN part_occurrences po ON po.id = f.part_occurrence_id
            JOIN part_numbers pn ON pn.id = f.part_number_id
            JOIN vehicle_configurations v ON v.id = f.vehicle_configuration_id
            JOIN diagrams d ON d.id = f.diagram_id
            LEFT JOIN taxonomy_nodes tn ON tn.id = d.taxonomy_node_id
            JOIN record_sources rs ON rs.record_type = 'fitment' AND rs.record_id = f.id
            JOIN http_responses hr ON hr.id = rs.response_id
            LEFT JOIN archive_captures ac ON ac.response_id = hr.id
            WHERE (f.id > ? OR (f.id = ? AND rs.id > ?))
              AND (? = 1 OR f.is_verified = 1)
            ORDER BY f.id, rs.id
            LIMIT ?
            """,
                (
                    fitment_id,
                    fitment_id,
                    source_id,
                    int(include_unverified_fitments),
                    EXPORT_BATCH_SIZE,
                ),
            )
            raw_rows = [dict(row) for row in await cursor.fetchall()]
            if not raw_rows:
                break
            yield [
                self._fitment_row(
                    row,
                    include_sensitive_source_urls=include_sensitive_source_urls,
                )
                for row in raw_rows
            ]
            last = raw_rows[-1]
            fitment_id = int(last["fitment_id"])
            source_id = int(last["source_record_id"])

        if include_compatibility_hints:
            hint_id = 0
            hint_source_id = 0
            while True:
                cursor = await self.repository.connection.execute(
                    """
                    SELECT h.id AS hint_id, rs.id AS source_record_id,
                           h.brand_text, h.model_text, h.compatibility_text, h.source_url,
                           pn.name_en_raw, pn.number_raw, rs.response_id, hr.body_sha256
                    FROM compatibility_hints h
                    JOIN part_numbers pn ON pn.id = h.part_number_id
                    JOIN record_sources rs
                      ON rs.record_type = 'compatibility_hint' AND rs.record_id = h.id
                    JOIN http_responses hr ON hr.id = rs.response_id
                    WHERE h.id > ? OR (h.id = ? AND rs.id > ?)
                    ORDER BY h.id, rs.id
                    LIMIT ?
                    """,
                    (hint_id, hint_id, hint_source_id, EXPORT_BATCH_SIZE),
                )
                raw_rows = [dict(row) for row in await cursor.fetchall()]
                if not raw_rows:
                    break
                yield [
                    self._hint_row(
                        row,
                        include_sensitive_source_urls=include_sensitive_source_urls,
                    )
                    for row in raw_rows
                ]
                last = raw_rows[-1]
                hint_id = int(last["hint_id"])
                hint_source_id = int(last["source_record_id"])

    async def export(
        self,
        path: Path,
        *,
        include_compatibility_hints: bool = False,
        include_unverified_fitments: bool = False,
        include_sensitive_source_urls: bool = False,
    ) -> int:
        suffix = path.suffix.lower()
        if suffix not in {".csv", ".jsonl"}:
            raise ValueError("export output must end in .csv or .jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        csv_writer: csv.DictWriter[str] | None = None
        encoding = "utf-8-sig" if suffix == ".csv" else "utf-8"
        with path.open("w", encoding=encoding, newline="" if suffix == ".csv" else None) as handle:
            async for batch in self._batches(
                include_compatibility_hints=include_compatibility_hints,
                include_unverified_fitments=include_unverified_fitments,
                include_sensitive_source_urls=include_sensitive_source_urls,
            ):
                if suffix == ".jsonl":
                    for row in batch:
                        handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                else:
                    if csv_writer is None:
                        csv_writer = csv.DictWriter(handle, fieldnames=list(EXPORT_COLUMNS))
                        csv_writer.writeheader()
                    for row in batch:
                        csv_writer.writerow(
                            {key: spreadsheet_safe(value) for key, value in row.items()}
                        )
                count += len(batch)
        return count

    @staticmethod
    def _fitment_row(
        row: dict[str, Any],
        *,
        include_sensitive_source_urls: bool,
    ) -> dict[str, Any]:
        category_path = row["category_path"] or ""
        categories = category_path.split(" > ") if category_path else []
        values = (
            row["brand"],
            row["vehicle_name"],
            row["model"],
            row["prod_period"],
            row["production_from"],
            row["production_to"],
            row["part_name"],
            row["part_number"],
            categories[0] if len(categories) > 0 else None,
            categories[1] if len(categories) > 1 else None,
            categories[2] if len(categories) > 2 else None,
            category_path,
            row["part_range_raw"],
            row["part_from"],
            row["part_to"],
            row["diagram_range_raw"],
            row["part_condition_raw"],
            row["description_raw"],
            row["options_raw"],
            row["diagram_code_raw"],
            row["diagram_name_raw"],
            row["callout_raw"],
            row["quantity_raw"],
            row["note_raw"],
            (
                row["source_url"]
                if include_sensitive_source_urls
                else redact_sensitive_url(str(row["source_url"]))
            ),
            row["source_mode"],
            row["archive_source"],
            row["captured_at"],
            row["truncation_reason"],
            row["response_id"],
            row["body_sha256"],
            row["confidence"],
            row["derivation"],
            bool(row["is_verified"]),
        )
        return dict(zip(EXPORT_COLUMNS, values, strict=True))

    @staticmethod
    def _hint_row(
        row: dict[str, Any],
        *,
        include_sensitive_source_urls: bool,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {column: None for column in EXPORT_COLUMNS}
        item.update(
            {
                "Brand": row["brand_text"],
                "Name": row["model_text"],
                "產品英文名稱": row["name_en_raw"],
                "Number": row["number_raw"],
                "Note": row["compatibility_text"],
                "Source URL": (
                    row["source_url"]
                    if include_sensitive_source_urls
                    else redact_sensitive_url(str(row["source_url"]))
                ),
                "Source mode": "live_http",
                "Response ID": row["response_id"],
                "Response SHA-256": row["body_sha256"],
                "Confidence": 0.4,
                "Derivation": "search_compatibility_hint",
                "Verified fitment": False,
            }
        )
        return item
