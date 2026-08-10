from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, cast

import aiosqlite

from partsouq_crawler.db.repository import Repository, utc_now
from partsouq_crawler.models.records import ParsedPage, PartRecord, VehicleRecord
from partsouq_crawler.parsers.base import PARSER_VERSION
from partsouq_crawler.parsers.common import is_assembly_name, normalize_part_number
from partsouq_crawler.services.provenance import add_source, add_sources

MYSQL_BATCH_SIZE = 500


class IngestService:
    parser_name = "catalog_parser"

    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    async def ingest(
        self,
        *,
        run_id: int,
        response_id: int,
        source_url: str,
        parsed: ParsedPage,
        verified_fitments: bool = True,
        fitment_derivation: str = "genuine_catalog_diagram_part_occurrence",
    ) -> int:
        if getattr(self.repository, "backend_name", "sqlite") == "mysql":
            return await self._ingest_mysql(
                run_id=run_id,
                response_id=response_id,
                source_url=source_url,
                parsed=parsed,
                verified_fitments=verified_fitments,
                fitment_derivation=fitment_derivation,
            )

        return await self._ingest_sqlite(
            run_id=run_id,
            response_id=response_id,
            source_url=source_url,
            parsed=parsed,
            verified_fitments=verified_fitments,
            fitment_derivation=fitment_derivation,
        )

    async def _ingest_sqlite(
        self,
        *,
        run_id: int,
        response_id: int,
        source_url: str,
        parsed: ParsedPage,
        verified_fitments: bool,
        fitment_derivation: str,
    ) -> int:
        inserted = 0
        async with self.repository.transaction() as connection:
            sqlite_connection = cast(aiosqlite.Connection, connection)
            vehicle_id: int | None = None
            if parsed.vehicle is not None:
                vehicle_id, created = await self._vehicle(
                    sqlite_connection, parsed.vehicle, source_url, response_id
                )
                inserted += created

            taxonomy_ids: dict[tuple[str, ...], int] = {}
            if vehicle_id is not None:
                for taxonomy in parsed.taxonomies:
                    parent_id: int | None = None
                    for depth, name in enumerate(taxonomy.path):
                        path = taxonomy.path[: depth + 1]
                        node_id, created = await self._taxonomy(
                            sqlite_connection,
                            vehicle_id=vehicle_id,
                            parent_id=parent_id,
                            depth=depth,
                            code=taxonomy.codes[depth] if depth < len(taxonomy.codes) else None,
                            name=name,
                            path=path,
                            source_url=source_url,
                            response_id=response_id,
                        )
                        taxonomy_ids[path] = node_id
                        parent_id = node_id
                        inserted += created

            diagram_ids: dict[str, int] = {}
            if vehicle_id is not None:
                for diagram in parsed.diagrams:
                    taxonomy_id = taxonomy_ids.get(diagram.category_path)
                    if taxonomy_id is None and len(taxonomy_ids) == 1:
                        taxonomy_id = next(iter(taxonomy_ids.values()))
                    diagram_id, created = await self._diagram(
                        sqlite_connection,
                        vehicle_id=vehicle_id,
                        taxonomy_id=taxonomy_id,
                        code=diagram.code_raw,
                        name=diagram.name_raw,
                        range_raw=diagram.range_raw,
                        range_from=diagram.diagram_from,
                        range_to=diagram.diagram_to,
                        metadata=diagram.metadata,
                        source_url=source_url,
                        response_id=response_id,
                    )
                    diagram_ids[diagram.code_raw or f"id:{diagram_id}"] = diagram_id
                    if diagram.name_raw:
                        diagram_ids[f"name:{diagram.name_raw}"] = diagram_id
                    inserted += created

            for part in parsed.parts:
                part_id, created = await self._part_number(
                    sqlite_connection,
                    part=part,
                    fallback_brand=parsed.vehicle.catalog_brand if parsed.vehicle else None,
                    source_url=source_url,
                    response_id=response_id,
                )
                inserted += created
                selected_diagram_id = self._choose_diagram(part, diagram_ids)
                if vehicle_id is None or selected_diagram_id is None:
                    continue
                occurrence_id, created = await self._occurrence(
                    sqlite_connection,
                    part_id=part_id,
                    diagram_id=selected_diagram_id,
                    vehicle_id=vehicle_id,
                    part=part,
                    source_url=source_url,
                    response_id=response_id,
                )
                inserted += created
                _, created = await self._fitment(
                    sqlite_connection,
                    occurrence_id=occurrence_id,
                    part_id=part_id,
                    vehicle_id=vehicle_id,
                    diagram_id=selected_diagram_id,
                    part=part,
                    source_url=source_url,
                    response_id=response_id,
                    is_verified=verified_fitments,
                    derivation=fitment_derivation,
                )
                inserted += created

            for hint in parsed.compatibility_hints:
                hint_part = PartRecord(number_raw=hint.part_number_raw)
                part_id, created = await self._part_number(
                    sqlite_connection,
                    part=hint_part,
                    fallback_brand=hint.brand_text,
                    source_url=source_url,
                    response_id=response_id,
                )
                inserted += created
                row_id, created = await self._get_or_create(
                    sqlite_connection,
                    select_sql="""
                        SELECT id FROM compatibility_hints
                        WHERE part_number_id = ? AND compatibility_text = ? AND source_url = ?
                    """,
                    select_values=(part_id, hint.compatibility_text, source_url),
                    insert_sql="""
                        INSERT INTO compatibility_hints(
                            part_number_id, brand_text, model_text, compatibility_text,
                            source_url, observed_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    insert_values=(
                        part_id,
                        hint.brand_text,
                        hint.model_text,
                        hint.compatibility_text,
                        source_url,
                        utc_now(),
                    ),
                )
                await self._source(
                    sqlite_connection,
                    "compatibility_hint",
                    row_id,
                    response_id,
                    source_url,
                )
                inserted += created

            for relation in parsed.part_relations:
                relation_part = PartRecord(number_raw=relation.from_part_number_raw)
                part_id, created = await self._part_number(
                    sqlite_connection,
                    part=relation_part,
                    fallback_brand=None,
                    source_url=source_url,
                    response_id=response_id,
                )
                inserted += created
                row_id, created = await self._get_or_create(
                    sqlite_connection,
                    select_sql="""
                        SELECT id FROM part_relations
                        WHERE from_part_number_id = ? AND to_part_number_raw = ?
                          AND relation_type = ? AND source_url = ?
                    """,
                    select_values=(
                        part_id,
                        relation.to_part_number_raw,
                        relation.relation_type,
                        source_url,
                    ),
                    insert_sql="""
                        INSERT INTO part_relations(
                            from_part_number_id, to_part_number_raw,
                            to_part_number_normalized, relation_type, relation_text,
                            confidence, source_url, observed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    insert_values=(
                        part_id,
                        relation.to_part_number_raw,
                        normalize_part_number(relation.to_part_number_raw),
                        relation.relation_type,
                        relation.relation_text,
                        relation.confidence,
                        source_url,
                        utc_now(),
                    ),
                )
                await self._source(
                    sqlite_connection, "part_relation", row_id, response_id, source_url
                )
                inserted += created

            await sqlite_connection.execute(
                """
                UPDATE crawl_runs
                SET records_extracted = records_extracted + ?, updated_at = ?
                WHERE id = ?
                """,
                (inserted, utc_now(), run_id),
            )
        return inserted

    async def _ingest_mysql(
        self,
        *,
        run_id: int,
        response_id: int,
        source_url: str,
        parsed: ParsedPage,
        verified_fitments: bool,
        fitment_derivation: str,
    ) -> int:
        inserted = 0
        now = utc_now()
        provenance: dict[tuple[str, int, int, str, str, str], None] = {}

        async with self.repository.transaction() as connection:
            vehicle_id: int | None = None
            if parsed.vehicle is not None:
                vehicle = parsed.vehicle
                vehicle_key = (
                    vehicle.catalog_brand,
                    vehicle.vehicle_external_id,
                    vehicle.model_raw,
                    vehicle.prod_period_raw,
                    source_url,
                )
                inserted += await self._mysql_insert_many(
                    connection,
                    """
                    INSERT IGNORE INTO vehicle_configurations(
                        catalog_brand, brand_raw, brand_normalized, name_raw, model_raw,
                        description_raw, options_raw, prod_period_raw, production_from,
                        production_to, production_precision, catalog_code,
                        vehicle_external_id, metadata_json, source_url, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            vehicle.catalog_brand,
                            vehicle.brand_raw,
                            vehicle.brand_normalized,
                            vehicle.name_raw,
                            vehicle.model_raw,
                            vehicle.description_raw,
                            vehicle.options_raw,
                            vehicle.prod_period_raw,
                            vehicle.production_from,
                            vehicle.production_to,
                            vehicle.production_precision,
                            vehicle.catalog_code,
                            vehicle.vehicle_external_id,
                            json.dumps(vehicle.metadata, sort_keys=True),
                            source_url,
                            now,
                            now,
                        )
                    ],
                )
                vehicle_ids = await self._mysql_select_ids(
                    connection,
                    table="vehicle_configurations",
                    columns=(
                        "catalog_brand",
                        "vehicle_external_id",
                        "model_raw",
                        "prod_period_raw",
                        "source_url",
                    ),
                    keys=[vehicle_key],
                )
                vehicle_id = vehicle_ids.get(vehicle_key)
                if vehicle_id is None:
                    raise RuntimeError("vehicle insert could not be resolved")
                vehicle_enrichment = (
                    vehicle.brand_raw,
                    vehicle.brand_normalized,
                    vehicle.name_raw,
                    vehicle.description_raw,
                    vehicle.options_raw,
                    vehicle.production_from,
                    vehicle.production_to,
                    vehicle.production_precision,
                    vehicle.catalog_code,
                )
                await connection.execute(
                    """
                    UPDATE vehicle_configurations
                    SET brand_raw = COALESCE(brand_raw, ?),
                        brand_normalized = COALESCE(brand_normalized, ?),
                        name_raw = COALESCE(name_raw, ?),
                        description_raw = COALESCE(description_raw, ?),
                        options_raw = COALESCE(options_raw, ?),
                        production_from = COALESCE(production_from, ?),
                        production_to = COALESCE(production_to, ?),
                        production_precision = CASE
                            WHEN production_precision IS NULL OR production_precision = 'unknown'
                            THEN ? ELSE production_precision
                        END,
                        catalog_code = COALESCE(catalog_code, ?),
                        updated_at = ?
                    WHERE id = ? AND (
                        (brand_raw IS NULL AND ? IS NOT NULL)
                        OR (brand_normalized IS NULL AND ? IS NOT NULL)
                        OR (name_raw IS NULL AND ? IS NOT NULL)
                        OR (description_raw IS NULL AND ? IS NOT NULL)
                        OR (options_raw IS NULL AND ? IS NOT NULL)
                        OR (production_from IS NULL AND ? IS NOT NULL)
                        OR (production_to IS NULL AND ? IS NOT NULL)
                        OR ((production_precision IS NULL OR production_precision = 'unknown')
                            AND ? <> 'unknown')
                        OR (catalog_code IS NULL AND ? IS NOT NULL)
                    )
                    """,
                    (*vehicle_enrichment, now, vehicle_id, *vehicle_enrichment),
                )
                provenance[
                    (
                        "vehicle_configuration",
                        vehicle_id,
                        response_id,
                        self.parser_name,
                        PARSER_VERSION,
                        source_url,
                    )
                ] = None

            taxonomy_ids: dict[tuple[str, ...], int] = {}
            if vehicle_id is not None:
                taxonomy_values: dict[tuple[str, ...], tuple[int, str | None, str, str]] = {}
                for taxonomy in parsed.taxonomies:
                    for depth, name in enumerate(taxonomy.path):
                        path = taxonomy.path[: depth + 1]
                        taxonomy_values.setdefault(
                            path,
                            (
                                depth,
                                taxonomy.codes[depth] if depth < len(taxonomy.codes) else None,
                                name,
                                " > ".join(path),
                            ),
                        )

                max_depth = max((value[0] for value in taxonomy_values.values()), default=-1)
                for depth in range(max_depth + 1):
                    paths = [path for path, value in taxonomy_values.items() if value[0] == depth]
                    rows: list[tuple[object, ...]] = []
                    keys: list[tuple[object, ...]] = []
                    for path in paths:
                        _, code, name, path_raw = taxonomy_values[path]
                        parent_id = taxonomy_ids.get(path[:-1]) if len(path) > 1 else None
                        rows.append(
                            (vehicle_id, parent_id, depth, code, name, path_raw, source_url)
                        )
                        keys.append((vehicle_id, path_raw))
                    inserted += await self._mysql_insert_many(
                        connection,
                        """
                        INSERT IGNORE INTO taxonomy_nodes(
                            vehicle_configuration_id, parent_id, depth, code_raw,
                            name_raw, path_raw, source_url
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
                    selected = await self._mysql_select_ids(
                        connection,
                        table="taxonomy_nodes",
                        columns=("vehicle_configuration_id", "path_raw"),
                        keys=keys,
                    )
                    for path, key in zip(paths, keys, strict=True):
                        node_id = selected.get(key)
                        if node_id is None:
                            raise RuntimeError("taxonomy insert could not be resolved")
                        taxonomy_ids[path] = node_id
                        provenance[
                            (
                                "taxonomy_node",
                                node_id,
                                response_id,
                                self.parser_name,
                                PARSER_VERSION,
                                source_url,
                            )
                        ] = None

            diagram_ids: dict[str, int] = {}
            diagram_key_by_index: dict[int, tuple[object, ...]] = {}
            if vehicle_id is not None:
                diagram_values: dict[tuple[object, ...], tuple[object, ...]] = {}
                for index, diagram in enumerate(parsed.diagrams):
                    taxonomy_id = taxonomy_ids.get(diagram.category_path)
                    if taxonomy_id is None and len(taxonomy_ids) == 1:
                        taxonomy_id = next(iter(taxonomy_ids.values()))
                    key = (
                        vehicle_id,
                        diagram.code_raw,
                        diagram.name_raw,
                        diagram.range_raw,
                        source_url,
                    )
                    diagram_key_by_index[index] = key
                    diagram_values.setdefault(
                        key,
                        (
                            vehicle_id,
                            taxonomy_id,
                            diagram.code_raw,
                            diagram.name_raw,
                            diagram.range_raw,
                            diagram.diagram_from,
                            diagram.diagram_to,
                            json.dumps(diagram.metadata, sort_keys=True),
                            source_url,
                        ),
                    )
                inserted += await self._mysql_insert_many(
                    connection,
                    """
                    INSERT IGNORE INTO diagrams(
                        vehicle_configuration_id, taxonomy_node_id, diagram_code_raw,
                        diagram_name_raw, diagram_range_raw, diagram_from, diagram_to,
                        metadata_json, source_url
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    list(diagram_values.values()),
                )
                selected = await self._mysql_select_ids(
                    connection,
                    table="diagrams",
                    columns=(
                        "vehicle_configuration_id",
                        "diagram_code_raw",
                        "diagram_name_raw",
                        "diagram_range_raw",
                        "source_url",
                    ),
                    keys=list(diagram_values),
                )
                diagram_enrichments: list[tuple[object, ...]] = []
                for index, diagram in enumerate(parsed.diagrams):
                    diagram_id = selected.get(diagram_key_by_index[index])
                    if diagram_id is None:
                        raise RuntimeError("diagram insert could not be resolved")
                    diagram_taxonomy_id = diagram_values[diagram_key_by_index[index]][1]
                    if isinstance(diagram_taxonomy_id, int):
                        diagram_enrichments.append((diagram_taxonomy_id, diagram_id))
                    diagram_ids[diagram.code_raw or f"id:{diagram_id}"] = diagram_id
                    if diagram.name_raw:
                        diagram_ids[f"name:{diagram.name_raw}"] = diagram_id
                    provenance[
                        (
                            "diagram",
                            diagram_id,
                            response_id,
                            self.parser_name,
                            PARSER_VERSION,
                            source_url,
                        )
                    ] = None
                if diagram_enrichments:
                    await connection.executemany(
                        """
                        UPDATE diagrams
                        SET taxonomy_node_id = COALESCE(taxonomy_node_id, ?)
                        WHERE id = ?
                        """,
                        diagram_enrichments,
                    )

            part_inputs: list[tuple[PartRecord, str | None]] = [
                (part, parsed.vehicle.catalog_brand if parsed.vehicle else None)
                for part in parsed.parts
            ]
            part_inputs.extend(
                (PartRecord(number_raw=hint.part_number_raw), hint.brand_text)
                for hint in parsed.compatibility_hints
            )
            part_inputs.extend(
                (PartRecord(number_raw=relation.from_part_number_raw), None)
                for relation in parsed.part_relations
            )
            part_values: dict[tuple[object, ...], tuple[object, ...]] = {}
            for part, fallback_brand in part_inputs:
                brand = part.part_brand_raw or fallback_brand
                key = (brand, part.number_raw)
                assembly, reason = is_assembly_name(part.name_en_raw)
                part_values.setdefault(
                    key,
                    (
                        brand,
                        part.number_raw,
                        normalize_part_number(part.number_raw),
                        part.name_en_raw,
                        assembly,
                        reason,
                        source_url,
                        now,
                        now,
                    ),
                )

            part_ids = await self._mysql_select_ids(
                connection,
                table="part_numbers",
                columns=("part_brand_raw", "number_raw"),
                keys=list(part_values),
            )
            missing_part_values = [
                values for key, values in part_values.items() if key not in part_ids
            ]
            inserted += await self._mysql_insert_many(
                connection,
                """
                INSERT IGNORE INTO part_numbers(
                    part_brand_raw, number_raw, number_normalized, name_en_raw,
                    is_assembly_inferred, assembly_inference_reason, source_url,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                missing_part_values,
            )
            part_ids = await self._mysql_select_ids(
                connection,
                table="part_numbers",
                columns=("part_brand_raw", "number_raw"),
                keys=list(part_values),
            )
            part_enrichments: list[tuple[object, ...]] = []
            for key in part_values:
                part_id = part_ids.get(key)
                if part_id is None:
                    raise RuntimeError("part number insert could not be resolved")
                values = part_values[key]
                if values[3] is not None:
                    part_enrichments.append(
                        (
                            values[3],
                            values[4],
                            values[5],
                            now,
                            part_id,
                            values[3],
                            values[4],
                            values[5],
                        )
                    )
                provenance[
                    (
                        "part_number",
                        part_id,
                        response_id,
                        self.parser_name,
                        PARSER_VERSION,
                        source_url,
                    )
                ] = None
            if part_enrichments:
                await connection.executemany(
                    """
                    UPDATE part_numbers
                    SET name_en_raw = COALESCE(name_en_raw, ?),
                        is_assembly_inferred = GREATEST(is_assembly_inferred, ?),
                        assembly_inference_reason = COALESCE(assembly_inference_reason, ?),
                        updated_at = ?
                    WHERE id = ? AND (
                        (name_en_raw IS NULL AND ? IS NOT NULL)
                        OR (is_assembly_inferred = 0 AND ? = 1)
                        OR (assembly_inference_reason IS NULL AND ? IS NOT NULL)
                    )
                    """,
                    part_enrichments,
                )

            occurrence_values: dict[tuple[object, ...], tuple[object, ...]] = {}
            occurrence_key_by_index: dict[int, tuple[object, ...]] = {}
            if vehicle_id is not None:
                fallback_brand = parsed.vehicle.catalog_brand if parsed.vehicle else None
                for index, part in enumerate(parsed.parts):
                    part_id = part_ids[(part.part_brand_raw or fallback_brand, part.number_raw)]
                    diagram_id = self._choose_diagram(part, diagram_ids)
                    if diagram_id is None:
                        continue
                    key = (
                        part_id,
                        diagram_id,
                        part.callout_raw,
                        part.quantity_raw,
                        part.part_range_raw,
                        part.condition_raw,
                        part.note_raw,
                        source_url,
                    )
                    occurrence_key_by_index[index] = key
                    occurrence_values.setdefault(
                        key,
                        (
                            part_id,
                            diagram_id,
                            vehicle_id,
                            part.callout_raw,
                            part.quantity_raw,
                            part.part_range_raw,
                            part.part_from,
                            part.part_to,
                            part.condition_raw,
                            part.note_raw,
                            json.dumps(part.row_metadata, sort_keys=True),
                            source_url,
                        ),
                    )
            occurrence_columns = (
                "part_number_id",
                "diagram_id",
                "callout_raw",
                "quantity_raw",
                "part_range_raw",
                "part_condition_raw",
                "note_raw",
                "source_url",
            )
            existing_occurrence_ids = await self._mysql_select_ids(
                connection,
                table="part_occurrences",
                columns=occurrence_columns,
                keys=list(occurrence_values),
            )
            null_condition_keys = [
                (*key[:5], None, *key[6:])
                for key in occurrence_values
                if key[5] is not None and key not in existing_occurrence_ids
            ]
            null_condition_ids = await self._mysql_select_ids(
                connection,
                table="part_occurrences",
                columns=occurrence_columns,
                keys=null_condition_keys,
            )
            upgraded_occurrence_ids: set[int] = set()
            occurrence_upgrades: list[tuple[object, ...]] = []
            for key, values in occurrence_values.items():
                if key[5] is None or key in existing_occurrence_ids:
                    continue
                old_id = null_condition_ids.get((*key[:5], None, *key[6:]))
                if old_id is None or old_id in upgraded_occurrence_ids:
                    continue
                upgraded_occurrence_ids.add(old_id)
                occurrence_upgrades.append((key[5], values[6], values[7], values[10], old_id))
            if occurrence_upgrades:
                await connection.executemany(
                    """
                    UPDATE IGNORE part_occurrences
                    SET part_condition_raw = ?,
                        part_from = COALESCE(part_from, ?),
                        part_to = COALESCE(part_to, ?),
                        row_metadata_json = ?
                    WHERE id = ? AND part_condition_raw IS NULL
                    """,
                    occurrence_upgrades,
                )

            inserted += await self._mysql_insert_many(
                connection,
                """
                INSERT IGNORE INTO part_occurrences(
                    part_number_id, diagram_id, vehicle_configuration_id,
                    callout_raw, quantity_raw, part_range_raw, part_from, part_to,
                    part_condition_raw, note_raw, row_metadata_json, source_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                list(occurrence_values.values()),
            )
            occurrence_ids = await self._mysql_select_ids(
                connection,
                table="part_occurrences",
                columns=occurrence_columns,
                keys=list(occurrence_values),
            )
            occurrence_enrichments: list[tuple[object, ...]] = []
            for key, values in occurrence_values.items():
                occurrence_id = occurrence_ids.get(key)
                if occurrence_id is not None:
                    occurrence_enrichments.append((values[6], values[7], values[10], occurrence_id))
            if occurrence_enrichments:
                await connection.executemany(
                    """
                    UPDATE part_occurrences
                    SET part_from = COALESCE(part_from, ?),
                        part_to = COALESCE(part_to, ?),
                        row_metadata_json = ?
                    WHERE id = ?
                    """,
                    occurrence_enrichments,
                )
            for occurrence_id in occurrence_ids.values():
                provenance[
                    (
                        "part_occurrence",
                        occurrence_id,
                        response_id,
                        self.parser_name,
                        PARSER_VERSION,
                        source_url,
                    )
                ] = None

            fitment_values: dict[tuple[object, ...], tuple[object, ...]] = {}
            for index, occurrence_key in occurrence_key_by_index.items():
                resolved_occurrence_id = occurrence_ids.get(occurrence_key)
                if resolved_occurrence_id is None or vehicle_id is None:
                    raise RuntimeError("part occurrence insert could not be resolved")
                part = parsed.parts[index]
                fitment_part_id = occurrence_key[0]
                fitment_diagram_id = occurrence_key[1]
                if not isinstance(fitment_part_id, int) or not isinstance(fitment_diagram_id, int):
                    raise RuntimeError("part occurrence has invalid foreign keys")
                fitment_values.setdefault(
                    (resolved_occurrence_id, fitment_derivation),
                    (
                        resolved_occurrence_id,
                        fitment_part_id,
                        vehicle_id,
                        fitment_diagram_id,
                        verified_fitments,
                        fitment_derivation,
                        1.0 if verified_fitments else 0.7,
                        part.part_from,
                        part.part_to,
                        source_url,
                    ),
                )
            inserted += await self._mysql_insert_many(
                connection,
                """
                INSERT IGNORE INTO fitments(
                    part_occurrence_id, part_number_id, vehicle_configuration_id,
                    diagram_id, is_verified, derivation, confidence,
                    effective_from, effective_to, source_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                list(fitment_values.values()),
            )
            fitment_ids = await self._mysql_select_ids(
                connection,
                table="fitments",
                columns=("part_occurrence_id", "derivation"),
                keys=list(fitment_values),
            )
            fitment_enrichments: list[tuple[object, ...]] = []
            for key, values in fitment_values.items():
                fitment_id = fitment_ids.get(key)
                if fitment_id is None:
                    continue
                fitment_enrichments.append((values[7], values[8], fitment_id))
                provenance[
                    (
                        "fitment",
                        fitment_id,
                        response_id,
                        self.parser_name,
                        PARSER_VERSION,
                        source_url,
                    )
                ] = None
            if fitment_enrichments:
                await connection.executemany(
                    """
                    UPDATE fitments
                    SET effective_from = COALESCE(effective_from, ?),
                        effective_to = COALESCE(effective_to, ?)
                    WHERE id = ?
                    """,
                    fitment_enrichments,
                )

            hint_values: dict[tuple[object, ...], tuple[object, ...]] = {}
            for hint in parsed.compatibility_hints:
                part_id = part_ids[(hint.brand_text, hint.part_number_raw)]
                key = (part_id, hint.compatibility_text, source_url)
                hint_values.setdefault(
                    key,
                    (
                        part_id,
                        hint.brand_text,
                        hint.model_text,
                        hint.compatibility_text,
                        source_url,
                        now,
                    ),
                )
            inserted += await self._mysql_insert_many(
                connection,
                """
                INSERT IGNORE INTO compatibility_hints(
                    part_number_id, brand_text, model_text, compatibility_text,
                    source_url, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                list(hint_values.values()),
            )
            hint_ids = await self._mysql_select_ids(
                connection,
                table="compatibility_hints",
                columns=("part_number_id", "compatibility_text", "source_url"),
                keys=list(hint_values),
            )
            for hint_id in hint_ids.values():
                provenance[
                    (
                        "compatibility_hint",
                        hint_id,
                        response_id,
                        self.parser_name,
                        PARSER_VERSION,
                        source_url,
                    )
                ] = None

            relation_values: dict[tuple[object, ...], tuple[object, ...]] = {}
            for relation in parsed.part_relations:
                part_id = part_ids[(None, relation.from_part_number_raw)]
                key = (
                    part_id,
                    relation.to_part_number_raw,
                    relation.relation_type,
                    source_url,
                )
                relation_values.setdefault(
                    key,
                    (
                        part_id,
                        relation.to_part_number_raw,
                        normalize_part_number(relation.to_part_number_raw),
                        relation.relation_type,
                        relation.relation_text,
                        relation.confidence,
                        source_url,
                        now,
                    ),
                )
            inserted += await self._mysql_insert_many(
                connection,
                """
                INSERT IGNORE INTO part_relations(
                    from_part_number_id, to_part_number_raw,
                    to_part_number_normalized, relation_type, relation_text,
                    confidence, source_url, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                list(relation_values.values()),
            )
            relation_ids = await self._mysql_select_ids(
                connection,
                table="part_relations",
                columns=(
                    "from_part_number_id",
                    "to_part_number_raw",
                    "relation_type",
                    "source_url",
                ),
                keys=list(relation_values),
            )
            for relation_id in relation_ids.values():
                provenance[
                    (
                        "part_relation",
                        relation_id,
                        response_id,
                        self.parser_name,
                        PARSER_VERSION,
                        source_url,
                    )
                ] = None

            await add_sources(connection, sources=list(provenance))
            await connection.execute(
                """
                UPDATE crawl_runs
                SET records_extracted = records_extracted + ?, updated_at = ?
                WHERE id = ?
                """,
                (inserted, utc_now(), run_id),
            )
        return inserted

    @staticmethod
    async def _mysql_insert_many(
        connection: Any,
        sql: str,
        rows: Sequence[tuple[object, ...]],
    ) -> int:
        if not rows:
            return 0
        inserted = 0
        for start in range(0, len(rows), MYSQL_BATCH_SIZE):
            cursor = await connection.executemany(sql, rows[start : start + MYSQL_BATCH_SIZE])
            rowcount = getattr(cursor, "rowcount", 0)
            if isinstance(rowcount, int) and rowcount > 0:
                inserted += rowcount
        return inserted

    @staticmethod
    async def _mysql_select_ids(
        connection: Any,
        *,
        table: str,
        columns: tuple[str, ...],
        keys: Sequence[tuple[object, ...]],
    ) -> dict[tuple[object, ...], int]:
        unique_keys = list(dict.fromkeys(keys))
        selected: dict[tuple[object, ...], int] = {}
        for start in range(0, len(unique_keys), MYSQL_BATCH_SIZE):
            batch = unique_keys[start : start + MYSQL_BATCH_SIZE]
            predicates = " OR ".join(
                "(" + " AND ".join(f"{column} <=> ?" for column in columns) + ")" for _ in batch
            )
            sql = f"SELECT id, {', '.join(columns)} FROM {table} WHERE {predicates} ORDER BY id"
            parameters = tuple(value for key in batch for value in key)
            cursor = await connection.execute(sql, parameters)
            rows = await cursor.fetchall()
            for row in rows:
                if isinstance(row, Mapping):
                    row_id = int(row["id"])
                    key = tuple(row[column] for column in columns)
                else:
                    row_id = int(row[0])
                    key = tuple(row[index + 1] for index in range(len(columns)))
                selected.setdefault(key, row_id)
        return selected

    async def _vehicle(
        self,
        connection: aiosqlite.Connection,
        vehicle: VehicleRecord,
        source_url: str,
        response_id: int,
    ) -> tuple[int, int]:
        now = utc_now()
        row_id, created = await self._get_or_create(
            connection,
            select_sql="""
                SELECT id FROM vehicle_configurations
                WHERE catalog_brand IS ? AND vehicle_external_id IS ?
                  AND model_raw IS ? AND prod_period_raw IS ? AND source_url = ?
            """,
            select_values=(
                vehicle.catalog_brand,
                vehicle.vehicle_external_id,
                vehicle.model_raw,
                vehicle.prod_period_raw,
                source_url,
            ),
            insert_sql="""
                INSERT INTO vehicle_configurations(
                    catalog_brand, brand_raw, brand_normalized, name_raw, model_raw,
                    description_raw, options_raw, prod_period_raw, production_from,
                    production_to, production_precision, catalog_code,
                    vehicle_external_id, metadata_json, source_url, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            insert_values=(
                vehicle.catalog_brand,
                vehicle.brand_raw,
                vehicle.brand_normalized,
                vehicle.name_raw,
                vehicle.model_raw,
                vehicle.description_raw,
                vehicle.options_raw,
                vehicle.prod_period_raw,
                vehicle.production_from,
                vehicle.production_to,
                vehicle.production_precision,
                vehicle.catalog_code,
                vehicle.vehicle_external_id,
                json.dumps(vehicle.metadata, sort_keys=True),
                source_url,
                now,
                now,
            ),
        )
        vehicle_enrichment = (
            vehicle.brand_raw,
            vehicle.brand_normalized,
            vehicle.name_raw,
            vehicle.description_raw,
            vehicle.options_raw,
            vehicle.production_from,
            vehicle.production_to,
            vehicle.production_precision,
            vehicle.catalog_code,
        )
        await connection.execute(
            """
            UPDATE vehicle_configurations
            SET brand_raw = COALESCE(brand_raw, ?),
                brand_normalized = COALESCE(brand_normalized, ?),
                name_raw = COALESCE(name_raw, ?),
                description_raw = COALESCE(description_raw, ?),
                options_raw = COALESCE(options_raw, ?),
                production_from = COALESCE(production_from, ?),
                production_to = COALESCE(production_to, ?),
                production_precision = CASE
                    WHEN production_precision IS NULL OR production_precision = 'unknown'
                    THEN ? ELSE production_precision
                END,
                catalog_code = COALESCE(catalog_code, ?),
                updated_at = ?
            WHERE id = ? AND (
                (brand_raw IS NULL AND ? IS NOT NULL)
                OR (brand_normalized IS NULL AND ? IS NOT NULL)
                OR (name_raw IS NULL AND ? IS NOT NULL)
                OR (description_raw IS NULL AND ? IS NOT NULL)
                OR (options_raw IS NULL AND ? IS NOT NULL)
                OR (production_from IS NULL AND ? IS NOT NULL)
                OR (production_to IS NULL AND ? IS NOT NULL)
                OR ((production_precision IS NULL OR production_precision = 'unknown')
                    AND ? <> 'unknown')
                OR (catalog_code IS NULL AND ? IS NOT NULL)
            )
            """,
            (*vehicle_enrichment, now, row_id, *vehicle_enrichment),
        )
        await self._source(connection, "vehicle_configuration", row_id, response_id, source_url)
        return row_id, created

    async def _taxonomy(
        self,
        connection: aiosqlite.Connection,
        *,
        vehicle_id: int,
        parent_id: int | None,
        depth: int,
        code: str | None,
        name: str,
        path: tuple[str, ...],
        source_url: str,
        response_id: int,
    ) -> tuple[int, int]:
        path_raw = " > ".join(path)
        row_id, created = await self._get_or_create(
            connection,
            select_sql="""
                SELECT id FROM taxonomy_nodes
                WHERE vehicle_configuration_id = ? AND path_raw = ?
            """,
            select_values=(vehicle_id, path_raw),
            insert_sql="""
                INSERT INTO taxonomy_nodes(
                    vehicle_configuration_id, parent_id, depth, code_raw,
                    name_raw, path_raw, source_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            insert_values=(vehicle_id, parent_id, depth, code, name, path_raw, source_url),
        )
        await self._source(connection, "taxonomy_node", row_id, response_id, source_url)
        return row_id, created

    async def _diagram(
        self,
        connection: aiosqlite.Connection,
        *,
        vehicle_id: int,
        taxonomy_id: int | None,
        code: str | None,
        name: str | None,
        range_raw: str | None,
        range_from: str | None,
        range_to: str | None,
        metadata: dict[str, Any],
        source_url: str,
        response_id: int,
    ) -> tuple[int, int]:
        row_id, created = await self._get_or_create(
            connection,
            select_sql="""
                SELECT id FROM diagrams
                WHERE vehicle_configuration_id = ? AND diagram_code_raw IS ?
                  AND diagram_name_raw IS ? AND diagram_range_raw IS ? AND source_url = ?
            """,
            select_values=(vehicle_id, code, name, range_raw, source_url),
            insert_sql="""
                INSERT INTO diagrams(
                    vehicle_configuration_id, taxonomy_node_id, diagram_code_raw,
                    diagram_name_raw, diagram_range_raw, diagram_from, diagram_to,
                    metadata_json, source_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            insert_values=(
                vehicle_id,
                taxonomy_id,
                code,
                name,
                range_raw,
                range_from,
                range_to,
                json.dumps(metadata, sort_keys=True),
                source_url,
            ),
        )
        if taxonomy_id is not None:
            await connection.execute(
                """
                UPDATE diagrams
                SET taxonomy_node_id = COALESCE(taxonomy_node_id, ?)
                WHERE id = ?
                """,
                (taxonomy_id, row_id),
            )
        await self._source(connection, "diagram", row_id, response_id, source_url)
        return row_id, created

    async def _part_number(
        self,
        connection: aiosqlite.Connection,
        *,
        part: PartRecord,
        fallback_brand: str | None,
        source_url: str,
        response_id: int,
    ) -> tuple[int, int]:
        brand = part.part_brand_raw or fallback_brand
        assembly, reason = is_assembly_name(part.name_en_raw)
        now = utc_now()
        row_id, created = await self._get_or_create(
            connection,
            select_sql="""
                SELECT id FROM part_numbers
                WHERE part_brand_raw IS ? AND number_raw = ?
                ORDER BY id LIMIT 1
            """,
            select_values=(brand, part.number_raw),
            insert_sql="""
                INSERT INTO part_numbers(
                    part_brand_raw, number_raw, number_normalized, name_en_raw,
                    is_assembly_inferred, assembly_inference_reason, source_url,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            insert_values=(
                brand,
                part.number_raw,
                normalize_part_number(part.number_raw),
                part.name_en_raw,
                assembly,
                reason,
                source_url,
                now,
                now,
            ),
        )
        if part.name_en_raw is not None:
            await connection.execute(
                """
                UPDATE part_numbers
                SET name_en_raw = COALESCE(name_en_raw, ?),
                    is_assembly_inferred = MAX(is_assembly_inferred, ?),
                    assembly_inference_reason = COALESCE(assembly_inference_reason, ?),
                    updated_at = ?
                WHERE id = ? AND (
                    (name_en_raw IS NULL AND ? IS NOT NULL)
                    OR (is_assembly_inferred = 0 AND ? = 1)
                    OR (assembly_inference_reason IS NULL AND ? IS NOT NULL)
                )
                """,
                (
                    part.name_en_raw,
                    assembly,
                    reason,
                    now,
                    row_id,
                    part.name_en_raw,
                    assembly,
                    reason,
                ),
            )
        await self._source(connection, "part_number", row_id, response_id, source_url)
        return row_id, created

    async def _occurrence(
        self,
        connection: aiosqlite.Connection,
        *,
        part_id: int,
        diagram_id: int,
        vehicle_id: int,
        part: PartRecord,
        source_url: str,
        response_id: int,
    ) -> tuple[int, int]:
        if part.condition_raw is not None:
            await connection.execute(
                """
                UPDATE OR IGNORE part_occurrences
                SET part_condition_raw = ?,
                    part_from = COALESCE(part_from, ?),
                    part_to = COALESCE(part_to, ?),
                    row_metadata_json = ?
                WHERE part_number_id = ? AND diagram_id = ? AND callout_raw IS ?
                  AND quantity_raw IS ? AND part_range_raw IS ?
                  AND part_condition_raw IS NULL AND note_raw IS ? AND source_url = ?
                """,
                (
                    part.condition_raw,
                    part.part_from,
                    part.part_to,
                    json.dumps(part.row_metadata, sort_keys=True),
                    part_id,
                    diagram_id,
                    part.callout_raw,
                    part.quantity_raw,
                    part.part_range_raw,
                    part.note_raw,
                    source_url,
                ),
            )
        row_id, created = await self._get_or_create(
            connection,
            select_sql="""
                SELECT id FROM part_occurrences
                WHERE part_number_id = ? AND diagram_id = ? AND callout_raw IS ?
                  AND quantity_raw IS ? AND part_range_raw IS ?
                  AND part_condition_raw IS ? AND note_raw IS ? AND source_url = ?
            """,
            select_values=(
                part_id,
                diagram_id,
                part.callout_raw,
                part.quantity_raw,
                part.part_range_raw,
                part.condition_raw,
                part.note_raw,
                source_url,
            ),
            insert_sql="""
                INSERT INTO part_occurrences(
                    part_number_id, diagram_id, vehicle_configuration_id,
                    callout_raw, quantity_raw, part_range_raw, part_from, part_to,
                    part_condition_raw, note_raw, row_metadata_json, source_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            insert_values=(
                part_id,
                diagram_id,
                vehicle_id,
                part.callout_raw,
                part.quantity_raw,
                part.part_range_raw,
                part.part_from,
                part.part_to,
                part.condition_raw,
                part.note_raw,
                json.dumps(part.row_metadata, sort_keys=True),
                source_url,
            ),
        )
        await connection.execute(
            """
            UPDATE part_occurrences
            SET part_from = COALESCE(part_from, ?),
                part_to = COALESCE(part_to, ?),
                row_metadata_json = ?
            WHERE id = ?
            """,
            (
                part.part_from,
                part.part_to,
                json.dumps(part.row_metadata, sort_keys=True),
                row_id,
            ),
        )
        await self._source(connection, "part_occurrence", row_id, response_id, source_url)
        return row_id, created

    async def _fitment(
        self,
        connection: aiosqlite.Connection,
        *,
        occurrence_id: int,
        part_id: int,
        vehicle_id: int,
        diagram_id: int,
        part: PartRecord,
        source_url: str,
        response_id: int,
        is_verified: bool,
        derivation: str,
    ) -> tuple[int, int]:
        row_id, created = await self._get_or_create(
            connection,
            select_sql="SELECT id FROM fitments WHERE part_occurrence_id = ? AND derivation = ?",
            select_values=(occurrence_id, derivation),
            insert_sql="""
                INSERT INTO fitments(
                    part_occurrence_id, part_number_id, vehicle_configuration_id,
                    diagram_id, is_verified, derivation, confidence,
                    effective_from, effective_to, source_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            insert_values=(
                occurrence_id,
                part_id,
                vehicle_id,
                diagram_id,
                is_verified,
                derivation,
                1.0 if is_verified else 0.7,
                part.part_from,
                part.part_to,
                source_url,
            ),
        )
        await connection.execute(
            """
            UPDATE fitments
            SET effective_from = COALESCE(effective_from, ?),
                effective_to = COALESCE(effective_to, ?)
            WHERE id = ?
            """,
            (part.part_from, part.part_to, row_id),
        )
        await self._source(connection, "fitment", row_id, response_id, source_url)
        return row_id, created

    @staticmethod
    def _choose_diagram(part: PartRecord, diagram_ids: dict[str, int]) -> int | None:
        if part.diagram_code_raw and part.diagram_code_raw in diagram_ids:
            return diagram_ids[part.diagram_code_raw]
        if part.diagram_name_raw:
            diagram_id = diagram_ids.get(f"name:{part.diagram_name_raw}")
            if diagram_id is not None:
                return diagram_id
        unique_ids = set(diagram_ids.values())
        if len(unique_ids) == 1:
            return next(iter(unique_ids))
        return None

    async def _source(
        self,
        connection: aiosqlite.Connection,
        record_type: str,
        record_id: int,
        response_id: int,
        source_url: str,
    ) -> None:
        await add_source(
            connection,
            record_type=record_type,
            record_id=record_id,
            response_id=response_id,
            parser_name=self.parser_name,
            parser_version=PARSER_VERSION,
            source_url=source_url,
        )

    @staticmethod
    async def _get_or_create(
        connection: aiosqlite.Connection,
        *,
        select_sql: str,
        select_values: tuple[object, ...],
        insert_sql: str,
        insert_values: tuple[object, ...],
    ) -> tuple[int, int]:
        cursor = await connection.execute(select_sql, select_values)
        row = await cursor.fetchone()
        if row is not None:
            return int(row["id"]), 0
        cursor = await connection.execute(insert_sql, insert_values)
        row_id = int(cursor.lastrowid or 0)
        if not row_id:
            raise RuntimeError("insert did not return an id")
        return row_id, 1
