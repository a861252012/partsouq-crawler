from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from partsouq_crawler.admin.db import Database
from partsouq_crawler.services.archive_queue import redact_sensitive_url

MAX_PAGE_SIZE = 100
FANOUT_LIMIT = 100


class AdminDataError(ValueError):
    pass


class RecordNotFoundError(AdminDataError):
    pass


class RevisionConflictError(AdminDataError):
    pass


@dataclass(frozen=True, slots=True)
class EntitySpec:
    key: str
    title: str
    table: str
    record_type: str
    source_fields: tuple[str, ...]
    editable_fields: tuple[str, ...]
    search_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PageCursor:
    kind_order: int
    sort_id: int

    def encode(self) -> str:
        raw = json.dumps([self.kind_order, self.sort_id], separators=(",", ":"))
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, value: str | None) -> PageCursor:
        if not value:
            return cls(-1, 0)
        try:
            padding = "=" * (-len(value) % 4)
            raw = base64.urlsafe_b64decode(value + padding)
            kind_order, sort_id = json.loads(raw)
            if kind_order not in (0, 1) or not isinstance(sort_id, int) or sort_id < 1:
                raise ValueError
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            raise AdminDataError("無效的分頁游標") from error
        return cls(kind_order, sort_id)


@dataclass(frozen=True, slots=True)
class RecordView:
    entity_type: str
    identity_key: str
    source_record_id: int | None
    manual_uuid: str | None
    payload: dict[str, Any]
    source_payload: dict[str, Any] | None
    status: str
    revision: int
    base_sha256: str
    updated_at: object | None


@dataclass(frozen=True, slots=True)
class RecordPage:
    records: tuple[RecordView, ...]
    next_cursor: str | None
    query: str
    include_retired: bool


@dataclass(frozen=True, slots=True)
class RecordDetail:
    record: RecordView
    events: tuple[dict[str, Any], ...]
    events_truncated: bool
    provenance: tuple[dict[str, Any], ...]
    provenance_truncated: bool


_VEHICLE_FIELDS = (
    "catalog_brand",
    "brand_raw",
    "brand_normalized",
    "name_raw",
    "model_raw",
    "description_raw",
    "options_raw",
    "prod_period_raw",
    "production_from",
    "production_to",
    "production_precision",
    "catalog_code",
    "vehicle_external_id",
    "metadata_json",
    "source_url",
    "created_at",
    "updated_at",
)
_DIAGRAM_FIELDS = (
    "vehicle_configuration_id",
    "taxonomy_node_id",
    "diagram_code_raw",
    "diagram_name_raw",
    "diagram_range_raw",
    "diagram_from",
    "diagram_to",
    "metadata_json",
    "source_url",
)
_PART_NUMBER_FIELDS = (
    "part_brand_raw",
    "number_raw",
    "number_normalized",
    "name_en_raw",
    "is_assembly_inferred",
    "assembly_inference_reason",
    "source_url",
    "created_at",
    "updated_at",
)
_OCCURRENCE_FIELDS = (
    "part_number_id",
    "diagram_id",
    "vehicle_configuration_id",
    "callout_raw",
    "quantity_raw",
    "part_range_raw",
    "part_from",
    "part_to",
    "part_condition_raw",
    "note_raw",
    "row_metadata_json",
    "source_url",
)
_FITMENT_FIELDS = (
    "part_occurrence_id",
    "part_number_id",
    "vehicle_configuration_id",
    "diagram_id",
    "is_verified",
    "derivation",
    "confidence",
    "effective_from",
    "effective_to",
    "source_url",
)

ENTITY_SPECS: dict[str, EntitySpec] = {
    "vehicle_configurations": EntitySpec(
        key="vehicle_configurations",
        title="車型設定",
        table="vehicle_configurations",
        record_type="vehicle_configuration",
        source_fields=_VEHICLE_FIELDS,
        editable_fields=_VEHICLE_FIELDS[:-3],
        search_fields=("catalog_brand", "model_raw", "name_raw", "catalog_code"),
    ),
    "diagrams": EntitySpec(
        key="diagrams",
        title="分解圖",
        table="diagrams",
        record_type="diagram",
        source_fields=_DIAGRAM_FIELDS,
        editable_fields=_DIAGRAM_FIELDS[:-1],
        search_fields=("diagram_code_raw", "diagram_name_raw"),
    ),
    "part_numbers": EntitySpec(
        key="part_numbers",
        title="零件號碼",
        table="part_numbers",
        record_type="part_number",
        source_fields=_PART_NUMBER_FIELDS,
        editable_fields=_PART_NUMBER_FIELDS[:-3],
        search_fields=("part_brand_raw", "number_normalized", "name_en_raw"),
    ),
    "part_occurrences": EntitySpec(
        key="part_occurrences",
        title="零件出現紀錄",
        table="part_occurrences",
        record_type="part_occurrence",
        source_fields=_OCCURRENCE_FIELDS,
        editable_fields=_OCCURRENCE_FIELDS[:-1],
        search_fields=("callout_raw", "part_range_raw"),
    ),
    "fitments": EntitySpec(
        key="fitments",
        title="適用關係",
        table="fitments",
        record_type="fitment",
        source_fields=_FITMENT_FIELDS,
        editable_fields=_FITMENT_FIELDS[:-1],
        search_fields=("derivation", "effective_from", "effective_to"),
    ),
}


def entity_spec(entity_type: str) -> EntitySpec:
    try:
        return ENTITY_SPECS[entity_type]
    except KeyError as error:
        raise AdminDataError(f"不支援的資料類型：{entity_type}") from error


def canonical_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _json_default(value: object) -> str:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


class AdminRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def dashboard_counts(self) -> dict[str, dict[str, int]]:
        source_columns = ",\n".join(
            f"(SELECT COUNT(*) FROM {spec.table}) AS `{spec.key}`" for spec in ENTITY_SPECS.values()
        )
        source = (
            self.database.fetch_one(
                "dashboard.source-counts",
                f"SELECT {source_columns}",
            )
            or {}
        )
        override_rows = self.database.fetch_all(
            "dashboard.override-counts",
            """
            SELECT entity_type,
                   SUM(source_record_id IS NULL) AS manual_count,
                   SUM(source_record_id IS NOT NULL) AS override_count,
                   SUM(status = 'retired') AS retired_count
            FROM admin_override_heads
            GROUP BY entity_type
            """,
        )
        overrides = {str(row["entity_type"]): row for row in override_rows}
        return {
            key: {
                "source": int(source.get(key, 0)),
                "manual": int(overrides.get(key, {}).get("manual_count", 0)),
                "overrides": int(overrides.get(key, {}).get("override_count", 0)),
                "retired": int(overrides.get(key, {}).get("retired_count", 0)),
            }
            for key in ENTITY_SPECS
        }

    def list_records(
        self,
        entity_type: str,
        *,
        query: str = "",
        cursor: str | None = None,
        limit: int = 50,
        include_retired: bool = False,
    ) -> RecordPage:
        spec = entity_spec(entity_type)
        size = min(max(limit, 1), MAX_PAGE_SIZE)
        page_cursor = PageCursor.decode(cursor)
        keys = self._page_keys(spec, query.strip(), page_cursor, size, include_retired)
        has_more = len(keys) > size
        visible_keys = keys[:size]

        source_ids = [int(row["sort_id"]) for row in visible_keys if int(row["kind_order"]) == 0]
        manual_ids = [int(row["sort_id"]) for row in visible_keys if int(row["kind_order"]) == 1]
        source_rows = self._source_batch(spec, source_ids, size)
        manual_rows = self._manual_batch(spec, manual_ids, size)

        source_by_id = {int(row["id"]): self._source_record(spec, row) for row in source_rows}
        manual_by_id = {
            int(row["override_head_id"]): self._manual_record(spec, row) for row in manual_rows
        }
        records: list[RecordView] = []
        for key in visible_keys:
            kind = int(key["kind_order"])
            sort_id = int(key["sort_id"])
            record = source_by_id.get(sort_id) if kind == 0 else manual_by_id.get(sort_id)
            if record is not None:
                records.append(record)

        next_cursor = None
        if has_more and visible_keys:
            last = visible_keys[-1]
            next_cursor = PageCursor(int(last["kind_order"]), int(last["sort_id"])).encode()
        return RecordPage(tuple(records), next_cursor, query.strip(), include_retired)

    def _page_keys(
        self,
        spec: EntitySpec,
        query: str,
        cursor: PageCursor,
        limit: int,
        include_retired: bool,
    ) -> list[dict[str, Any]]:
        if query:
            return self._search_page_keys(spec, query, cursor, limit, include_retired)

        sql = f"""
            (
                SELECT 0 AS kind_order, s.id AS sort_id
                FROM {spec.table} AS s
                LEFT JOIN admin_override_heads AS h
                  ON h.entity_type = %s AND h.source_record_id = s.id
                WHERE (%s = 1 OR COALESCE(h.status, 'active') <> 'retired')
                  AND (%s = -1 OR (%s = 0 AND s.id < %s))
            )
            UNION ALL
            (
                SELECT 1 AS kind_order, h.id AS sort_id
                FROM admin_override_heads AS h
                WHERE h.entity_type = %s
                  AND h.source_record_id IS NULL
                  AND (%s = 1 OR h.status <> 'retired')
                  AND (%s IN (-1, 0) OR (%s = 1 AND h.id < %s))
            )
            ORDER BY kind_order ASC, sort_id DESC
            LIMIT %s
        """
        params: list[object] = [
            spec.key,
            int(include_retired),
            cursor.kind_order,
            cursor.kind_order,
            cursor.sort_id,
            spec.key,
            int(include_retired),
            cursor.kind_order,
            cursor.kind_order,
            cursor.sort_id,
            limit + 1,
        ]
        return self.database.fetch_all(f"list.keys.{spec.key}", sql, params)

    def _search_page_keys(
        self,
        spec: EntitySpec,
        query: str,
        cursor: PageCursor,
        limit: int,
        include_retired: bool,
    ) -> list[dict[str, Any]]:
        source_search_value = f"{query}%"
        override_search_value = f"%{query}%"
        candidate_sql = " UNION ".join(
            f"SELECT id FROM {spec.table} WHERE `{field}` LIKE %s" for field in spec.search_fields
        )
        effective_search = " OR ".join(
            "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(h.payload_json, '$."
            + field
            + "')), CAST(s.`"
            + field
            + "` AS CHAR)) LIKE %s"
            for field in spec.search_fields
        )
        sql = f"""
            (
                SELECT 0 AS kind_order, candidates.id AS sort_id
                FROM ({candidate_sql}) AS candidates
                WHERE NOT EXISTS (
                    SELECT 1 FROM admin_override_heads AS existing
                    WHERE existing.entity_type = %s
                      AND existing.source_record_id = candidates.id
                )
                  AND (%s = -1 OR (%s = 0 AND candidates.id < %s))
            )
            UNION ALL
            (
                SELECT 0 AS kind_order, s.id AS sort_id
                FROM admin_override_heads AS h
                JOIN {spec.table} AS s ON s.id = h.source_record_id
                WHERE h.entity_type = %s
                  AND h.source_record_id IS NOT NULL
                  AND (%s = 1 OR h.status <> 'retired')
                  AND ({effective_search})
                  AND (%s = -1 OR (%s = 0 AND s.id < %s))
            )
            UNION ALL
            (
                SELECT 1 AS kind_order, h.id AS sort_id
                FROM admin_override_heads AS h
                WHERE h.entity_type = %s
                  AND h.source_record_id IS NULL
                  AND CAST(h.payload_json AS CHAR) LIKE %s
                  AND (%s = 1 OR h.status <> 'retired')
                  AND (%s IN (-1, 0) OR (%s = 1 AND h.id < %s))
            )
            ORDER BY kind_order ASC, sort_id DESC
            LIMIT %s
        """
        params: list[object] = [
            *([source_search_value] * len(spec.search_fields)),
            spec.key,
            cursor.kind_order,
            cursor.kind_order,
            cursor.sort_id,
            spec.key,
            int(include_retired),
            *([source_search_value] * len(spec.search_fields)),
            cursor.kind_order,
            cursor.kind_order,
            cursor.sort_id,
            spec.key,
            override_search_value,
            int(include_retired),
            cursor.kind_order,
            cursor.kind_order,
            cursor.sort_id,
            limit + 1,
        ]
        return self.database.fetch_all(f"list.keys.{spec.key}", sql, params)

    def _source_batch(
        self,
        spec: EntitySpec,
        source_ids: list[int],
        page_size: int,
    ) -> list[dict[str, Any]]:
        padded_ids = [*source_ids, *([0] * (page_size - len(source_ids)))]
        placeholders = ", ".join(["%s"] * page_size)
        fields = ", ".join(f"s.`{field}`" for field in spec.source_fields)
        return self.database.fetch_all(
            f"list.source-batch.{spec.key}",
            f"""
            SELECT s.id, {fields},
                   h.id AS override_head_id, h.identity_key, h.manual_uuid,
                   h.payload_json AS override_payload_json, h.status AS override_status,
                   h.revision AS override_revision, h.base_sha256 AS override_base_sha256,
                   h.updated_at AS override_updated_at
            FROM {spec.table} AS s
            LEFT JOIN admin_override_heads AS h
              ON h.entity_type = %s AND h.source_record_id = s.id
            WHERE s.id IN ({placeholders})
            """,
            [spec.key, *padded_ids],
        )

    def _manual_batch(
        self,
        spec: EntitySpec,
        head_ids: list[int],
        page_size: int,
    ) -> list[dict[str, Any]]:
        padded_ids = [*head_ids, *([0] * (page_size - len(head_ids)))]
        placeholders = ", ".join(["%s"] * page_size)
        return self.database.fetch_all(
            f"list.manual-batch.{spec.key}",
            f"""
            SELECT id AS override_head_id, identity_key, source_record_id, manual_uuid,
                   payload_json AS override_payload_json, status AS override_status,
                   revision AS override_revision, base_sha256 AS override_base_sha256,
                   updated_at AS override_updated_at
            FROM admin_override_heads
            WHERE entity_type = %s AND source_record_id IS NULL
              AND id IN ({placeholders})
            """,
            [spec.key, *padded_ids],
        )

    def get_record(self, entity_type: str, identity_key: str) -> RecordDetail:
        spec = entity_spec(entity_type)
        source_id, manual_uuid = self._parse_identity(identity_key)
        base = self._detail_base(spec, source_id or 0)
        head = self.database.fetch_one(
            f"detail.head.{spec.key}",
            """
            SELECT id AS override_head_id, identity_key, source_record_id, manual_uuid,
                   payload_json AS override_payload_json, status AS override_status,
                   revision AS override_revision, base_sha256 AS override_base_sha256,
                   updated_at AS override_updated_at
            FROM admin_override_heads
            WHERE entity_type = %s AND identity_key = %s
            """,
            (spec.key, identity_key),
        )
        if source_id is not None:
            if base is None:
                raise RecordNotFoundError("找不到來源資料")
            combined = {**base, **(head or {})}
            record = self._source_record(spec, combined)
        else:
            if head is None or str(head.get("manual_uuid")) != manual_uuid:
                raise RecordNotFoundError("找不到人工資料")
            record = self._manual_record(spec, head)

        head_id = int(head["override_head_id"]) if head else 0
        events = self.database.fetch_all(
            f"detail.events.{spec.key}",
            """
            SELECT id, action, revision, base_sha256, before_json, after_json,
                   actor, reason, created_at
            FROM admin_override_events
            WHERE head_id = %s
            ORDER BY revision DESC
            LIMIT %s
            """,
            (head_id, FANOUT_LIMIT + 1),
        )
        provenance = self.database.fetch_all(
            f"detail.provenance.{spec.key}",
            """
            SELECT rs.id, rs.parser_name, rs.parser_version, rs.source_url,
                   rs.extracted_at, rs.response_id, hr.http_status,
                   hr.body_sha256, hr.fetched_at, ac.archive_source,
                   ac.collection_name, ac.captured_at
            FROM record_sources AS rs
            JOIN http_responses AS hr ON hr.id = rs.response_id
            LEFT JOIN archive_captures AS ac ON ac.response_id = hr.id
            WHERE rs.record_type = %s AND rs.record_id = %s
            ORDER BY rs.id DESC
            LIMIT %s
            """,
            (spec.record_type, source_id or 0, FANOUT_LIMIT + 1),
        )
        return RecordDetail(
            record=record,
            events=tuple(self._decode_row(row) for row in events[:FANOUT_LIMIT]),
            events_truncated=len(events) > FANOUT_LIMIT,
            provenance=tuple(self._decode_row(row) for row in provenance[:FANOUT_LIMIT]),
            provenance_truncated=len(provenance) > FANOUT_LIMIT,
        )

    def _detail_base(self, spec: EntitySpec, source_id: int) -> dict[str, Any] | None:
        fields = ", ".join(f"`{field}`" for field in spec.source_fields)
        return self.database.fetch_one(
            f"detail.base.{spec.key}",
            f"SELECT id, {fields} FROM {spec.table} WHERE id = %s",
            (source_id,),
        )

    def create_manual(
        self,
        entity_type: str,
        payload: dict[str, Any],
        *,
        actor: str,
        reason: str,
    ) -> str:
        spec = entity_spec(entity_type)
        cleaned = self._clean_payload(spec, payload)
        actor, reason = self._audit_fields(actor, reason)
        manual_uuid = str(uuid.uuid4())
        identity_key = f"manual:{manual_uuid}"
        empty_sha = canonical_sha256({})
        encoded = self._json(cleaned)
        with self.database.transaction():
            result = self.database.execute(
                f"write.create-head.{spec.key}",
                """
                INSERT INTO admin_override_heads(
                    entity_type, identity_key, source_record_id, manual_uuid,
                    payload_json, status, revision, base_sha256,
                    actor, reason, created_at, updated_at
                ) VALUES (%s, %s, NULL, %s, %s, 'active', 1, %s,
                          %s, %s, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))
                """,
                (spec.key, identity_key, manual_uuid, encoded, empty_sha, actor, reason),
            )
            self._insert_event(
                spec,
                head_id=result.lastrowid,
                identity_key=identity_key,
                source_record_id=None,
                manual_uuid=manual_uuid,
                action="create",
                revision=1,
                base_sha256=empty_sha,
                before=None,
                after=cleaned,
                actor=actor,
                reason=reason,
            )
        return identity_key

    def update_record(
        self,
        entity_type: str,
        identity_key: str,
        payload: dict[str, Any],
        *,
        expected_revision: int,
        actor: str,
        reason: str,
    ) -> int:
        return self._change_record(
            entity_type,
            identity_key,
            action="update",
            payload=payload,
            expected_revision=expected_revision,
            actor=actor,
            reason=reason,
        )

    def retire_record(
        self,
        entity_type: str,
        identity_key: str,
        *,
        expected_revision: int,
        actor: str,
        reason: str,
    ) -> int:
        return self._change_record(
            entity_type,
            identity_key,
            action="retire",
            payload=None,
            expected_revision=expected_revision,
            actor=actor,
            reason=reason,
        )

    def restore_record(
        self,
        entity_type: str,
        identity_key: str,
        *,
        expected_revision: int,
        actor: str,
        reason: str,
    ) -> int:
        return self._change_record(
            entity_type,
            identity_key,
            action="restore",
            payload=None,
            expected_revision=expected_revision,
            actor=actor,
            reason=reason,
        )

    def _change_record(
        self,
        entity_type: str,
        identity_key: str,
        *,
        action: str,
        payload: dict[str, Any] | None,
        expected_revision: int,
        actor: str,
        reason: str,
    ) -> int:
        spec = entity_spec(entity_type)
        source_id, manual_uuid = self._parse_identity(identity_key)
        actor, reason = self._audit_fields(actor, reason)
        with self.database.transaction():
            base = self._locked_base(spec, source_id or 0)
            head = self.database.fetch_one(
                f"write.lock-head.{spec.key}",
                """
                SELECT id AS override_head_id, identity_key, source_record_id, manual_uuid,
                       payload_json AS override_payload_json, status AS override_status,
                       revision AS override_revision, base_sha256 AS override_base_sha256
                FROM admin_override_heads
                WHERE entity_type = %s AND identity_key = %s
                FOR UPDATE
                """,
                (spec.key, identity_key),
            )
            if source_id is not None and base is None:
                raise RecordNotFoundError("找不到來源資料")
            if source_id is None and head is None:
                raise RecordNotFoundError("找不到人工資料")

            current_revision = int(head.get("override_revision", 0)) if head else 0
            if current_revision != expected_revision:
                raise RevisionConflictError(
                    f"資料已被修改；預期版本 {expected_revision}，目前版本 {current_revision}"
                )
            source_payload = self._source_payload(spec, base) if base else {}
            current_override = self._json_object(head.get("override_payload_json")) if head else {}
            before = {**source_payload, **current_override}
            status = str(head.get("override_status", "active")) if head else "active"

            if action == "update":
                if payload is None:
                    raise AdminDataError("更新內容不可為空")
                next_payload = self._clean_payload(spec, payload)
                next_status = status
            else:
                next_payload = current_override
                next_status = "retired" if action == "retire" else "active"
                if action == "retire" and status == "retired":
                    raise AdminDataError("資料已停用")
                if action == "restore" and status != "retired":
                    raise AdminDataError("資料目前不是停用狀態")

            after = {**source_payload, **next_payload}
            next_revision = current_revision + 1
            base_sha256 = canonical_sha256(source_payload)
            encoded_payload = self._json(next_payload)
            if head is None:
                result = self.database.execute(
                    f"write.insert-head.{spec.key}",
                    """
                    INSERT INTO admin_override_heads(
                        entity_type, identity_key, source_record_id, manual_uuid,
                        payload_json, status, revision, base_sha256,
                        actor, reason, created_at, updated_at
                    ) VALUES (%s, %s, %s, NULL, %s, %s, %s, %s,
                              %s, %s, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))
                    """,
                    (
                        spec.key,
                        identity_key,
                        source_id,
                        encoded_payload,
                        next_status,
                        next_revision,
                        base_sha256,
                        actor,
                        reason,
                    ),
                )
                head_id = result.lastrowid
            else:
                head_id = int(head["override_head_id"])
                result = self.database.execute(
                    f"write.update-head.{spec.key}",
                    """
                    UPDATE admin_override_heads
                    SET payload_json = %s, status = %s, revision = %s,
                        base_sha256 = %s, actor = %s, reason = %s,
                        updated_at = UTC_TIMESTAMP(6)
                    WHERE id = %s AND revision = %s
                    """,
                    (
                        encoded_payload,
                        next_status,
                        next_revision,
                        base_sha256,
                        actor,
                        reason,
                        head_id,
                        current_revision,
                    ),
                )
                if result.rowcount != 1:
                    raise RevisionConflictError("資料版本衝突，請重新載入")

            self._insert_event(
                spec,
                head_id=head_id,
                identity_key=identity_key,
                source_record_id=source_id,
                manual_uuid=manual_uuid,
                action=action,
                revision=next_revision,
                base_sha256=base_sha256,
                before=before,
                after=after,
                actor=actor,
                reason=reason,
            )
        return next_revision

    def _locked_base(self, spec: EntitySpec, source_id: int) -> dict[str, Any] | None:
        fields = ", ".join(f"`{field}`" for field in spec.source_fields)
        return self.database.fetch_one(
            f"write.lock-base.{spec.key}",
            f"SELECT id, {fields} FROM {spec.table} WHERE id = %s FOR SHARE",
            (source_id,),
        )

    def _insert_event(
        self,
        spec: EntitySpec,
        *,
        head_id: int,
        identity_key: str,
        source_record_id: int | None,
        manual_uuid: str | None,
        action: str,
        revision: int,
        base_sha256: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        actor: str,
        reason: str,
    ) -> None:
        self.database.execute(
            f"write.append-event.{spec.key}",
            """
            INSERT INTO admin_override_events(
                head_id, entity_type, identity_key, source_record_id, manual_uuid,
                action, revision, base_sha256, before_json, after_json,
                actor, reason, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, UTC_TIMESTAMP(6))
            """,
            (
                head_id,
                spec.key,
                identity_key,
                source_record_id,
                manual_uuid,
                action,
                revision,
                base_sha256,
                self._json(before) if before is not None else None,
                self._json(after) if after is not None else None,
                actor,
                reason,
            ),
        )

    @staticmethod
    def _clean_payload(spec: EntitySpec, payload: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(payload) - set(spec.editable_fields))
        if unknown:
            raise AdminDataError(f"不可編輯欄位：{', '.join(unknown)}")
        cleaned = {field: payload[field] for field in spec.editable_fields if field in payload}
        if not cleaned:
            raise AdminDataError("至少要提供一個可編輯欄位")
        return cleaned

    @staticmethod
    def _audit_fields(actor: str, reason: str) -> tuple[str, str]:
        actor = actor.strip()
        reason = reason.strip()
        if not actor or not reason:
            raise AdminDataError("操作者與修改原因都是必填")
        if len(actor) > 191:
            raise AdminDataError("操作者名稱過長")
        return actor, reason

    @staticmethod
    def _parse_identity(identity_key: str) -> tuple[int | None, str | None]:
        if identity_key.startswith("source:"):
            try:
                source_id = int(identity_key.removeprefix("source:"))
            except ValueError as error:
                raise AdminDataError("無效的來源資料識別碼") from error
            if source_id < 1:
                raise AdminDataError("無效的來源資料識別碼")
            return source_id, None
        if identity_key.startswith("manual:"):
            manual_uuid = identity_key.removeprefix("manual:")
            try:
                parsed = str(uuid.UUID(manual_uuid))
            except ValueError as error:
                raise AdminDataError("無效的人工資料識別碼") from error
            return None, parsed
        raise AdminDataError("無效的資料識別碼")

    @classmethod
    def _source_record(cls, spec: EntitySpec, row: dict[str, Any]) -> RecordView:
        source_payload = cls._source_payload(spec, row)
        override = cls._json_object(row.get("override_payload_json"))
        source_id = int(row["id"])
        return RecordView(
            entity_type=spec.key,
            identity_key=f"source:{source_id}",
            source_record_id=source_id,
            manual_uuid=None,
            payload=cls._display_mapping({**source_payload, **override}),
            source_payload=cls._display_mapping(source_payload),
            status=str(row.get("override_status") or "active"),
            revision=int(row.get("override_revision") or 0),
            base_sha256=str(row.get("override_base_sha256") or canonical_sha256(source_payload)),
            updated_at=row.get("override_updated_at") or row.get("updated_at"),
        )

    @classmethod
    def _manual_record(cls, spec: EntitySpec, row: dict[str, Any]) -> RecordView:
        payload = cls._json_object(row.get("override_payload_json"))
        return RecordView(
            entity_type=spec.key,
            identity_key=str(row["identity_key"]),
            source_record_id=None,
            manual_uuid=str(row["manual_uuid"]),
            payload=cls._display_mapping(payload),
            source_payload=None,
            status=str(row.get("override_status") or "active"),
            revision=int(row.get("override_revision") or 1),
            base_sha256=str(row.get("override_base_sha256") or canonical_sha256({})),
            updated_at=row.get("override_updated_at"),
        )

    @classmethod
    def _source_payload(cls, spec: EntitySpec, row: dict[str, Any]) -> dict[str, Any]:
        return {field: cls._decode_value(row.get(field)) for field in spec.source_fields}

    @classmethod
    def _decode_row(cls, row: dict[str, Any]) -> dict[str, Any]:
        return {key: cls._display_value(key, value) for key, value in row.items()}

    @classmethod
    def _display_mapping(cls, payload: dict[str, Any]) -> dict[str, Any]:
        return {key: cls._display_value(key, value) for key, value in payload.items()}

    @classmethod
    def _display_value(cls, key: str, value: Any) -> Any:
        decoded = cls._decode_value(value)
        if key == "source_url" and isinstance(decoded, str):
            return redact_sensitive_url(decoded)
        if isinstance(decoded, dict):
            return cls._display_mapping(decoded)
        if isinstance(decoded, list):
            return [cls._display_value("", item) for item in decoded]
        return decoded

    @staticmethod
    def _decode_value(value: Any) -> Any:
        if isinstance(value, str) and value[:1] in ("{", "["):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    @classmethod
    def _json_object(cls, value: Any) -> dict[str, Any]:
        decoded = cls._decode_value(value)
        return dict(decoded) if isinstance(decoded, dict) else {}

    @staticmethod
    def _json(payload: dict[str, Any] | None) -> str:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        )

    @staticmethod
    def record_as_dict(record: RecordView) -> dict[str, Any]:
        return asdict(record)
