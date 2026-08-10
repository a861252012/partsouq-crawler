from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from partsouq_crawler.crawl.rate_limit import HostRateLimiter
from partsouq_crawler.db.protocols import DatabaseRow
from partsouq_crawler.db.repository import Repository
from partsouq_crawler.logging import CrawlLogger
from partsouq_crawler.nhtsa.config import NhtsaConfig

VPIC_BATCH_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVINValuesBatch/"
VIN_PATTERN = re.compile(r"[A-HJ-NPR-Z0-9]{17}")
MAX_VIN_BATCH = 50


class VinDecodeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VinDecodeBatch:
    http_status: int
    response_headers: Mapping[str, str]
    body: bytes
    results: tuple[Mapping[str, object], ...]


class VinDecoder(Protocol):
    async def decode(self, vins: Sequence[str]) -> VinDecodeBatch: ...


class NhtsaVinDecoder:
    def __init__(self, config: NhtsaConfig) -> None:
        self.config = config
        self.curl_executable = shutil.which("curl")
        self.rate_limiter = HostRateLimiter(config.api_delay_seconds)

    async def __aenter__(self) -> NhtsaVinDecoder:
        if self.curl_executable is None:
            raise VinDecodeError("curl is required for the NHTSA VIN batch endpoint")
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def decode(self, vins: Sequence[str]) -> VinDecodeBatch:
        if not 1 <= len(vins) <= MAX_VIN_BATCH:
            raise ValueError("NHTSA VIN batch must contain between 1 and 50 VINs")
        if any(VIN_PATTERN.fullmatch(vin) is None for vin in vins):
            raise ValueError("NHTSA VIN batch contains an invalid 17-character VIN")
        if self.curl_executable is None:
            raise VinDecodeError("curl is required for the NHTSA VIN batch endpoint")
        form_data = ";".join(f"{vin}," for vin in vins)
        marker = b"\n__NHTSA_VIN_HTTP_STATUS__:"
        await self.rate_limiter.wait()
        descriptor, header_name = tempfile.mkstemp(prefix="nhtsa-vin-headers-")
        os.close(descriptor)
        header_path = Path(header_name)
        try:
            process = await asyncio.create_subprocess_exec(
                self.curl_executable,
                "--silent",
                "--show-error",
                "--max-time",
                str(max(1, math.ceil(self.config.request_timeout_seconds))),
                "--request",
                "POST",
                "--header",
                "Accept: application/json",
                "--header",
                "Content-Type: application/x-www-form-urlencoded",
                "--user-agent",
                self.config.user_agent,
                "--dump-header",
                str(header_path),
                "--config",
                "-",
                "--write-out",
                marker.decode() + "%{http_code}",
                VPIC_BATCH_URL,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate(
                f'data = "DATA={form_data}&format=json"\n'.encode("ascii")
            )
            raw_headers = await asyncio.to_thread(header_path.read_bytes)
        finally:
            await asyncio.to_thread(header_path.unlink, missing_ok=True)
        if process.returncode != 0:
            message = stderr.decode("utf-8", errors="replace").strip()
            raise VinDecodeError(f"NHTSA VIN curl failed ({process.returncode}): {message}")
        try:
            body, status_raw = stdout.rsplit(marker, 1)
            status = int(status_raw.decode("ascii"))
        except (ValueError, UnicodeDecodeError) as error:
            raise VinDecodeError("NHTSA VIN curl response is missing its HTTP status") from error
        headers = self._parse_response_headers(raw_headers)
        if status != 200:
            raise VinDecodeError(f"NHTSA VIN endpoint returned HTTP {status}")
        try:
            document = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise VinDecodeError("NHTSA VIN response is not valid UTF-8 JSON") from error
        if not isinstance(document, dict) or not isinstance(document.get("Results"), list):
            raise VinDecodeError("NHTSA VIN response does not contain a Results array")
        results = document["Results"]
        if not all(isinstance(item, dict) for item in results):
            raise VinDecodeError("NHTSA VIN response contains a non-object result")
        count = document.get("Count")
        if count is not None and count != len(results):
            raise VinDecodeError("NHTSA VIN response Count does not match Results")
        return VinDecodeBatch(
            http_status=status,
            response_headers=headers,
            body=body,
            results=tuple(cast(Mapping[str, object], item) for item in results),
        )

    @staticmethod
    def _parse_response_headers(raw_headers: bytes) -> dict[str, str]:
        for block in reversed(re.split(rb"\r?\n\r?\n", raw_headers.strip())):
            lines = block.splitlines()
            if not lines or not lines[0].startswith(b"HTTP/"):
                continue
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if b":" not in line:
                    continue
                name_raw, value_raw = line.split(b":", 1)
                name = name_raw.decode("ascii", errors="ignore").strip().lower()
                if not name:
                    continue
                value = value_raw.decode("latin-1").strip()
                if name == "set-cookie":
                    value = "[redacted]"
                headers[name] = f"{headers[name]}, {value}" if name in headers else value
            return headers
        raise VinDecodeError("NHTSA VIN response is missing its HTTP headers")


class StationCatalogService:
    def __init__(
        self,
        repository: Repository,
        nhtsa_config: NhtsaConfig,
        *,
        decoder: VinDecoder | None = None,
        logger: CrawlLogger | None = None,
        worker_id: str | None = None,
        lease_seconds: int = 900,
        max_attempts: int = 3,
    ) -> None:
        if repository.backend_name != "mysql":
            raise ValueError("station catalog sync requires MySQL")
        if lease_seconds < 30:
            raise ValueError("VIN decode lease must be at least 30 seconds")
        if max_attempts < 1:
            raise ValueError("VIN decode attempts must be positive")
        self.repository = repository
        self.nhtsa_config = nhtsa_config
        self.decoder = decoder
        self.logger = logger or CrawlLogger(json_mode=True)
        self.worker_id = worker_id or f"station:{uuid.uuid4().hex}"
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts

    async def run(self, *, run_key: str) -> dict[str, object]:
        if self.decoder is None:
            async with NhtsaVinDecoder(self.nhtsa_config) as decoder:
                vin_report = await self._decode_pending(decoder)
        else:
            vin_report = await self._decode_pending(self.decoder)
        term_count = await self._refresh_part_terms()
        projected_fitments = await self._project_linked_vin_fitments()
        reconciliation_count = await self._refresh_reconciliation_cases(run_key)
        status = "completed" if int(vin_report["failed"]) == 0 else "completed_with_gaps"
        report: dict[str, object] = {
            "status": status,
            "run_key": run_key,
            "part_terms_touched": term_count,
            "vin": vin_report,
            "vin_fitments_touched": projected_fitments,
            "reconciliation_cases_touched": reconciliation_count,
        }
        self.logger.event("station_sync_finished", **report)
        return report

    async def _refresh_part_terms(self) -> int:
        async with self.repository.transaction() as connection:
            updated = await connection.execute(
                """
                UPDATE part_term_mappings AS pt
                JOIN part_numbers AS pn ON pn.id = pt.part_number_id
                SET pt.name_en_raw = pn.name_en_raw,
                    pt.name_en_normalized = LOWER(TRIM(pn.name_en_raw)),
                    pt.source_url = pn.source_url,
                    pt.observed_at = pn.updated_at,
                    pt.updated_at = UTC_TIMESTAMP(6)
                WHERE pn.name_en_raw IS NOT NULL AND TRIM(pn.name_en_raw) <> ''
                  AND NOT (
                    pt.name_en_raw <=> pn.name_en_raw
                    AND pt.name_en_normalized <=> LOWER(TRIM(pn.name_en_raw))
                    AND pt.source_url <=> pn.source_url
                    AND pt.observed_at <=> pn.updated_at
                  )
                """
            )
            inserted = await connection.execute(
                """
                INSERT IGNORE INTO part_term_mappings(
                    part_number_id, name_en_raw, name_en_normalized,
                    name_zh_tw, common_names_zh_tw, mapping_status,
                    source_kind, confidence, source_url, observed_at,
                    created_at, updated_at
                )
                SELECT pn.id, pn.name_en_raw, LOWER(TRIM(pn.name_en_raw)),
                       NULL, JSON_ARRAY(), 'missing_translation',
                       'partsouq_catalog', 0.5, pn.source_url, pn.updated_at,
                       UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
                FROM part_numbers AS pn
                WHERE pn.name_en_raw IS NOT NULL AND TRIM(pn.name_en_raw) <> ''
                """
            )
            await connection.execute(
                """
                INSERT IGNORE INTO record_sources(
                    record_type, record_id, response_id, parser_name,
                    parser_version, source_url, extracted_at
                )
                SELECT 'part_term_mapping', pt.id, rs.response_id,
                       'station_term_projection', '1', rs.source_url, UTC_TIMESTAMP(6)
                FROM part_term_mappings AS pt
                JOIN record_sources AS rs
                  ON rs.record_type = 'part_number' AND rs.record_id = pt.part_number_id
                """
            )
        return max(updated.rowcount, 0) + max(inserted.rowcount, 0)

    async def _decode_pending(self, decoder: VinDecoder) -> dict[str, int]:
        decoded = 0
        failed = 0
        batches = 0
        while claims := await self._claim_vin_requests():
            batches += 1
            vins = [str(claim["vin"]) for claim in claims]
            self.logger.event("vin_decode_batch_started", batch=batches, count=len(vins))
            try:
                response = await decoder.decode(vins)
                completed, missing = await self._store_vin_batch(claims, response)
                decoded += completed
                failed += missing
            except Exception as error:
                failed += len(claims)
                await self._release_vin_requests(claims, f"{type(error).__name__}: {error}")
                self.logger.event(
                    "vin_decode_batch_failed",
                    batch=batches,
                    count=len(vins),
                    error_type=type(error).__name__,
                )
                break
        return {"batches": batches, "decoded": decoded, "failed": failed}

    async def _claim_vin_requests(self) -> list[dict[str, object]]:
        async with self.repository.transaction() as connection:
            cursor = await connection.execute(
                """
                SELECT id
                FROM vin_decode_requests
                WHERE attempts < ?
                  AND (
                    status = 'pending'
                    OR (status = 'in_progress' AND lease_expires_at < UTC_TIMESTAMP(6))
                  )
                ORDER BY id
                LIMIT ?
                FOR UPDATE SKIP LOCKED
                """,
                (self.max_attempts, MAX_VIN_BATCH),
            )
            ids = [int(row["id"]) for row in await cursor.fetchall()]
            if not ids:
                return []
            placeholders = ", ".join("?" for _ in ids)
            await connection.execute(
                f"""
                UPDATE vin_decode_requests
                SET status = 'in_progress', worker_id = ?,
                    fencing_token = fencing_token + 1,
                    attempts = attempts + 1,
                    lease_expires_at = DATE_ADD(UTC_TIMESTAMP(6), INTERVAL ? SECOND),
                    updated_at = UTC_TIMESTAMP(6), last_error = NULL
                WHERE id IN ({placeholders})
                """,  # noqa: S608 - placeholders are generated from claimed integer IDs.
                (self.worker_id, self.lease_seconds, *ids),
            )
            claimed = await connection.execute(
                f"""
                SELECT id, vin, attempts, fencing_token
                FROM vin_decode_requests
                WHERE id IN ({placeholders}) AND worker_id = ? AND status = 'in_progress'
                ORDER BY id
                """,  # noqa: S608 - placeholders are generated from claimed integer IDs.
                (*ids, self.worker_id),
            )
            return [dict(row) for row in await claimed.fetchall()]

    async def _store_vin_batch(
        self,
        claims: Sequence[Mapping[str, object]],
        response: VinDecodeBatch,
    ) -> tuple[int, int]:
        body_sha256 = hashlib.sha256(response.body).hexdigest()
        batch_identity = [
            [int(str(claim["id"])), int(str(claim["fencing_token"]))] for claim in claims
        ]
        batch_key = hashlib.sha256(
            json.dumps(batch_identity, separators=(",", ":")).encode()
        ).hexdigest()
        by_vin = {
            str(result.get("VIN") or "").strip().upper(): result for result in response.results
        }
        mapping_rows: list[tuple[object, ...]] = []
        missing_claims: list[Mapping[str, object]] = []
        for claim in claims:
            vin = str(claim["vin"])
            result = by_vin.get(vin)
            if result is None:
                missing_claims.append(claim)
                continue
            make = self._optional_text(result.get("Make"))
            model = self._optional_text(result.get("Model"))
            year = self._model_year(result.get("ModelYear"))
            has_vehicle = bool(make or model or year)
            mapping_rows.append(
                (
                    vin,
                    make,
                    model,
                    self._optional_text(result.get("Series")),
                    self._optional_text(result.get("BodyClass")),
                    self._optional_text(result.get("VehicleType")),
                    year,
                    self._optional_text(result.get("Manufacturer")),
                    (
                        "decoded"
                        if make and model and year
                        else "partial"
                        if has_vehicle
                        else "failed"
                    ),
                    self._optional_text(result.get("ErrorCode")),
                    self._optional_text(result.get("ErrorText")),
                )
            )
        async with self.repository.transaction() as connection:
            raw_cursor = await connection.execute(
                """
                INSERT INTO vin_decode_responses(
                    batch_key_sha256, http_status, response_headers_json,
                    body_sha256, body_json, response_bytes, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, UTC_TIMESTAMP(6))
                ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)
                """,
                (
                    batch_key,
                    response.http_status,
                    json.dumps(dict(response.response_headers), sort_keys=True),
                    body_sha256,
                    response.body,
                    len(response.body),
                ),
            )
            response_id = raw_cursor.lastrowid
            if response_id is None:
                existing = await connection.execute(
                    "SELECT id FROM vin_decode_responses WHERE batch_key_sha256 = ?",
                    (batch_key,),
                )
                row = await existing.fetchone()
                if row is None:
                    raise RuntimeError("VIN raw response was not stored")
                response_id = int(row["id"])
            if mapping_rows:
                await connection.executemany(
                    """
                    INSERT INTO vin_vehicle_mappings(
                        vin, make_name, model_name, series_name, body_class,
                        vehicle_type, model_year, manufacturer_name, decode_status,
                        error_code, error_text, source_kind, response_id,
                        decoded_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              'nhtsa_vpic', ?, UTC_TIMESTAMP(6),
                              UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))
                    ON DUPLICATE KEY UPDATE
                        make_name = VALUES(make_name), model_name = VALUES(model_name),
                        series_name = VALUES(series_name), body_class = VALUES(body_class),
                        vehicle_type = VALUES(vehicle_type), model_year = VALUES(model_year),
                        manufacturer_name = VALUES(manufacturer_name),
                        decode_status = VALUES(decode_status), error_code = VALUES(error_code),
                        error_text = VALUES(error_text), source_kind = VALUES(source_kind),
                        response_id = VALUES(response_id), decoded_at = UTC_TIMESTAMP(6),
                        updated_at = UTC_TIMESTAMP(6)
                    """,
                    [(*row, response_id) for row in mapping_rows],
                )
            vins = [str(claim["vin"]) for claim in claims if claim not in missing_claims]
            mapping_ids: dict[str, int] = {}
            if vins:
                placeholders = ", ".join("?" for _ in vins)
                mapping_cursor = await connection.execute(
                    f"SELECT id, vin FROM vin_vehicle_mappings WHERE vin IN ({placeholders})",
                    vins,
                )
                mapping_ids = {
                    str(row["vin"]): int(row["id"]) for row in await mapping_cursor.fetchall()
                }
            for claim in claims:
                vin = str(claim["vin"])
                mapping_id = mapping_ids.get(vin)
                if mapping_id is None:
                    continue
                finished = await connection.execute(
                    """
                    UPDATE vin_decode_requests
                    SET status = 'completed', mapping_id = ?, worker_id = NULL,
                        lease_expires_at = NULL, last_error = NULL,
                        updated_at = UTC_TIMESTAMP(6), finished_at = UTC_TIMESTAMP(6)
                    WHERE id = ? AND status = 'in_progress' AND worker_id = ?
                      AND fencing_token = ?
                    """,
                    (
                        mapping_id,
                        claim["id"],
                        self.worker_id,
                        claim["fencing_token"],
                    ),
                )
                if finished.rowcount != 1:
                    raise RuntimeError(f"VIN request lease lost before finish: {claim['id']}")
        if missing_claims:
            await self._release_vin_requests(missing_claims, "VIN missing from NHTSA Results")
        return len(mapping_rows), len(missing_claims)

    async def _release_vin_requests(
        self,
        claims: Sequence[Mapping[str, object]],
        error: str,
    ) -> None:
        async with self.repository.transaction() as connection:
            for claim in claims:
                terminal = int(str(claim["attempts"])) >= self.max_attempts
                cursor = await connection.execute(
                    """
                    UPDATE vin_decode_requests
                    SET status = ?, worker_id = NULL, lease_expires_at = NULL,
                        last_error = ?, updated_at = UTC_TIMESTAMP(6),
                        finished_at = IF(? = 'failed', UTC_TIMESTAMP(6), NULL)
                    WHERE id = ? AND status = 'in_progress' AND worker_id = ?
                      AND fencing_token = ?
                    """,
                    (
                        "failed" if terminal else "pending",
                        error[:16_000],
                        "failed" if terminal else "pending",
                        claim["id"],
                        self.worker_id,
                        claim["fencing_token"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"VIN request lease lost before release: {claim['id']}")

    async def _project_linked_vin_fitments(self) -> int:
        link_expression = """
            COALESCE(
                CAST(JSON_UNQUOTE(JSON_EXTRACT(
                    h.payload_json, '$.partsouq_vehicle_configuration_id'
                )) AS UNSIGNED),
                vm.partsouq_vehicle_configuration_id
            )
        """
        async with self.repository.transaction() as connection:
            await connection.execute(
                f"""
                DELETE rs
                FROM record_sources AS rs
                JOIN vin_part_fitments AS vpf
                  ON rs.record_type = 'vin_part_fitment' AND rs.record_id = vpf.id
                JOIN vin_vehicle_mappings AS vm
                  ON vm.id = vpf.vin_vehicle_mapping_id
                LEFT JOIN admin_override_heads AS h
                  ON h.entity_type = 'vin_vehicle_mappings'
                 AND h.source_record_id = vm.id AND h.status = 'active'
                LEFT JOIN admin_override_heads AS vpf_head
                  ON vpf_head.entity_type = 'vin_part_fitments'
                 AND vpf_head.source_record_id = vpf.id
                WHERE vpf.derivation = 'station_vehicle_link_to_partsouq_fitment'
                  AND vpf.is_verified = 0
                  AND vpf_head.id IS NULL
                  AND NOT (vpf.vehicle_configuration_id <=> {link_expression})
                """  # noqa: S608 - expression is a fixed application constant.
            )
            deleted = await connection.execute(
                f"""
                DELETE vpf
                FROM vin_part_fitments AS vpf
                JOIN vin_vehicle_mappings AS vm
                  ON vm.id = vpf.vin_vehicle_mapping_id
                LEFT JOIN admin_override_heads AS h
                  ON h.entity_type = 'vin_vehicle_mappings'
                 AND h.source_record_id = vm.id AND h.status = 'active'
                LEFT JOIN admin_override_heads AS vpf_head
                  ON vpf_head.entity_type = 'vin_part_fitments'
                 AND vpf_head.source_record_id = vpf.id
                WHERE vpf.derivation = 'station_vehicle_link_to_partsouq_fitment'
                  AND vpf.is_verified = 0
                  AND vpf_head.id IS NULL
                  AND NOT (vpf.vehicle_configuration_id <=> {link_expression})
                """  # noqa: S608 - expression is a fixed application constant.
            )
            cursor = await connection.execute(
                f"""
                INSERT INTO vin_part_fitments(
                    vin_vehicle_mapping_id, part_number_id, vehicle_configuration_id,
                    is_verified, derivation, confidence, source_url,
                    observed_at, created_at, updated_at
                )
                SELECT vm.id, f.part_number_id, f.vehicle_configuration_id,
                       0, 'station_vehicle_link_to_partsouq_fitment', f.confidence,
                       f.source_url, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
                FROM vin_vehicle_mappings AS vm
                LEFT JOIN admin_override_heads AS h
                  ON h.entity_type = 'vin_vehicle_mappings'
                 AND h.source_record_id = vm.id AND h.status = 'active'
                JOIN fitments AS f
                  ON f.vehicle_configuration_id = {link_expression}
                WHERE {link_expression} IS NOT NULL
                ON DUPLICATE KEY UPDATE
                    updated_at = IF(
                        vin_part_fitments.confidence <=> VALUES(confidence)
                        AND vin_part_fitments.source_url <=> VALUES(source_url),
                        vin_part_fitments.updated_at, UTC_TIMESTAMP(6)
                    ),
                    observed_at = IF(
                        vin_part_fitments.confidence <=> VALUES(confidence)
                        AND vin_part_fitments.source_url <=> VALUES(source_url),
                        vin_part_fitments.observed_at, UTC_TIMESTAMP(6)
                    ),
                    confidence = VALUES(confidence), source_url = VALUES(source_url)
                """  # noqa: S608 - expression is a fixed application constant.
            )
            await connection.execute(
                f"""
                INSERT IGNORE INTO record_sources(
                    record_type, record_id, response_id, parser_name,
                    parser_version, source_url, extracted_at
                )
                SELECT 'vin_part_fitment', vpf.id, rs.response_id,
                       'station_vin_fitment_projection', '1', rs.source_url,
                       UTC_TIMESTAMP(6)
                FROM vin_part_fitments AS vpf
                JOIN vin_vehicle_mappings AS vm
                  ON vm.id = vpf.vin_vehicle_mapping_id
                LEFT JOIN admin_override_heads AS h
                  ON h.entity_type = 'vin_vehicle_mappings'
                 AND h.source_record_id = vm.id AND h.status = 'active'
                JOIN fitments AS f
                  ON f.vehicle_configuration_id = {link_expression}
                 AND f.part_number_id = vpf.part_number_id
                JOIN record_sources AS rs
                  ON rs.record_type = 'fitment' AND rs.record_id = f.id
                WHERE vpf.derivation = 'station_vehicle_link_to_partsouq_fitment'
                """  # noqa: S608 - expression is a fixed application constant.
            )
        return max(deleted.rowcount, 0) + max(cursor.rowcount, 0)

    async def _refresh_reconciliation_cases(self, run_key: str) -> int:
        async with self.repository.transaction() as connection:
            missing_terms = await connection.execute(
                """
                INSERT IGNORE INTO reconciliation_cases(
                    case_key_sha256, case_type, subject_type, subject_key,
                    severity, status, current_json, candidate_json,
                    evidence_json, comments_json, assigned_to, resolution,
                    source_run_key, opened_at, updated_at, resolved_at
                )
                SELECT SHA2(CONCAT('missing_part_translation:', pt.id), 256),
                       'missing_part_translation', 'part_term_mapping', CAST(pt.id AS CHAR),
                       'medium', 'open',
                       JSON_OBJECT('name_en_raw', pt.name_en_raw,
                                   'name_zh_tw', pt.name_zh_tw,
                                   'common_names_zh_tw', pt.common_names_zh_tw),
                       JSON_OBJECT(),
                       JSON_OBJECT('part_number_id', pt.part_number_id,
                                   'source_url', pt.source_url),
                       JSON_ARRAY(), NULL, NULL, ?, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), NULL
                FROM part_term_mappings AS pt
                LEFT JOIN admin_override_heads AS h
                  ON h.entity_type = 'part_term_mappings'
                 AND h.source_record_id = pt.id AND h.status = 'active'
                WHERE NULLIF(TRIM(COALESCE(
                    JSON_UNQUOTE(JSON_EXTRACT(h.payload_json, '$.name_zh_tw')),
                    pt.name_zh_tw, ''
                )), '') IS NULL
                """,
                (run_key,),
            )
            missing_links = await connection.execute(
                """
                INSERT IGNORE INTO reconciliation_cases(
                    case_key_sha256, case_type, subject_type, subject_key,
                    severity, status, current_json, candidate_json,
                    evidence_json, comments_json, assigned_to, resolution,
                    source_run_key, opened_at, updated_at, resolved_at
                )
                SELECT SHA2(CONCAT('vin_partsouq_vehicle_link:', vm.id), 256),
                       'vin_partsouq_vehicle_link', 'vin_vehicle_mapping', CAST(vm.id AS CHAR),
                       'high', 'open',
                       JSON_OBJECT('vin', vm.vin, 'make', vm.make_name,
                                   'model', vm.model_name, 'year', vm.model_year),
                       JSON_OBJECT(),
                       JSON_OBJECT('nhtsa_response_id', vm.response_id),
                       JSON_ARRAY(), NULL, NULL, ?, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), NULL
                FROM vin_vehicle_mappings AS vm
                LEFT JOIN admin_override_heads AS h
                  ON h.entity_type = 'vin_vehicle_mappings'
                 AND h.source_record_id = vm.id AND h.status = 'active'
                WHERE vm.decode_status IN ('decoded', 'partial')
                  AND COALESCE(
                    CAST(JSON_UNQUOTE(JSON_EXTRACT(
                        h.payload_json, '$.partsouq_vehicle_configuration_id'
                    )) AS UNSIGNED),
                    vm.partsouq_vehicle_configuration_id
                  ) IS NULL
                """,
                (run_key,),
            )
            invalid_links = await connection.execute(
                """
                INSERT IGNORE INTO reconciliation_cases(
                    case_key_sha256, case_type, subject_type, subject_key,
                    severity, status, current_json, candidate_json,
                    evidence_json, comments_json, assigned_to, resolution,
                    source_run_key, opened_at, updated_at, resolved_at
                )
                SELECT SHA2(CONCAT('invalid_vin_partsouq_vehicle_link:', vm.id), 256),
                       'invalid_vin_partsouq_vehicle_link', 'vin_vehicle_mapping',
                       CAST(vm.id AS CHAR), 'high', 'open',
                       JSON_OBJECT('vin', vm.vin, 'make', vm.make_name,
                                   'model', vm.model_name, 'year', vm.model_year),
                       JSON_OBJECT('partsouq_vehicle_configuration_id',
                                   COALESCE(
                                       CAST(JSON_UNQUOTE(JSON_EXTRACT(
                                           h.payload_json,
                                           '$.partsouq_vehicle_configuration_id'
                                       )) AS UNSIGNED),
                                       vm.partsouq_vehicle_configuration_id
                                   )),
                       JSON_OBJECT('nhtsa_response_id', vm.response_id),
                       JSON_ARRAY(), NULL, NULL, ?, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), NULL
                FROM vin_vehicle_mappings AS vm
                LEFT JOIN admin_override_heads AS h
                  ON h.entity_type = 'vin_vehicle_mappings'
                 AND h.source_record_id = vm.id AND h.status = 'active'
                LEFT JOIN vehicle_configurations AS vc
                  ON vc.id = COALESCE(
                      CAST(JSON_UNQUOTE(JSON_EXTRACT(
                          h.payload_json, '$.partsouq_vehicle_configuration_id'
                      )) AS UNSIGNED),
                      vm.partsouq_vehicle_configuration_id
                  )
                WHERE vm.decode_status IN ('decoded', 'partial')
                  AND COALESCE(
                      CAST(JSON_UNQUOTE(JSON_EXTRACT(
                          h.payload_json, '$.partsouq_vehicle_configuration_id'
                      )) AS UNSIGNED),
                      vm.partsouq_vehicle_configuration_id
                  ) IS NOT NULL
                  AND vc.id IS NULL
                """,
                (run_key,),
            )
        return (
            max(missing_terms.rowcount, 0)
            + max(missing_links.rowcount, 0)
            + max(invalid_links.rowcount, 0)
        )

    @staticmethod
    def _optional_text(value: object) -> str | None:
        text = str(value).strip() if value is not None else ""
        return text or None

    @staticmethod
    def _model_year(value: object) -> int | None:
        text = str(value).strip() if value is not None else ""
        if not text.isdigit():
            return None
        year = int(text)
        return year if 1886 <= year <= 9998 else None


async def queued_vin_count(repository: Repository) -> int:
    cursor = await repository.connection.execute(
        "SELECT COUNT(*) AS count FROM vin_decode_requests WHERE status = ?",
        ("pending",),
    )
    row = cast(DatabaseRow | None, await cursor.fetchone())
    return int(row["count"]) if row is not None else 0
