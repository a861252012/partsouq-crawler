from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Sequence

import pytest

from partsouq_crawler.config import PartSouqMySQLConfig
from partsouq_crawler.db.repository import Repository
from partsouq_crawler.models.crawl import FetchResult
from partsouq_crawler.models.records import (
    DiagramRecord,
    ParsedPage,
    PartRecord,
    TaxonomyRecord,
    VehicleRecord,
)
from partsouq_crawler.nhtsa.config import NhtsaConfig
from partsouq_crawler.services.ingest import IngestService
from partsouq_crawler.services.station_catalog import (
    StationCatalogService,
    VinDecodeBatch,
)

pytestmark = pytest.mark.skipif(
    os.getenv("PARTSOUQ_TEST_MYSQL") != "1",
    reason="set PARTSOUQ_TEST_MYSQL=1 to run local MySQL integration tests",
)

TEST_VIN = "TEST0000000000000"


class FakeVinDecoder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def decode(self, vins: Sequence[str]) -> VinDecodeBatch:
        self.calls.append(tuple(vins))
        results = [
            {
                "VIN": vin,
                "Make": "HONDA",
                "Model": "CIVIC",
                "Series": "LX",
                "BodyClass": "Sedan/Saloon",
                "VehicleType": "PASSENGER CAR",
                "ModelYear": "1991",
                "Manufacturer": "AMERICAN HONDA MOTOR CO., INC.",
                "ErrorCode": "0",
                "ErrorText": "0 - VIN decoded clean.",
            }
            for vin in vins
        ]
        body = json.dumps({"Count": len(results), "Results": results}).encode()
        return VinDecodeBatch(200, {"content-type": "application/json"}, body, tuple(results))


def _config() -> PartSouqMySQLConfig:
    return PartSouqMySQLConfig.from_env(
        database=os.getenv("PARTSOUQ_TEST_MYSQL_DATABASE", "partsouq_test")
    )


def test_station_sync_decodes_vin_preserves_raw_response_and_is_idempotent() -> None:
    async def scenario() -> None:
        repository = await Repository.create_mysql(_config())
        decoder = FakeVinDecoder()
        prefix = uuid.uuid4().hex
        try:
            run_id = await repository.create_or_get_run(f"station-source-{prefix}", [], {})
            source_url = f"https://example.invalid/station-source/{prefix}"
            response_id, _ = await repository.store_response(
                run_id,
                None,
                FetchResult(
                    requested_url=source_url,
                    final_url=source_url,
                    status=200,
                    headers={"Content-Type": "text/html; charset=utf-8"},
                    body=f"<html>{prefix}</html>".encode(),
                    elapsed_ms=1,
                    attempt=1,
                ),
                challenged=False,
                challenge_reason=None,
            )
            await IngestService(repository).ingest(
                run_id=run_id,
                response_id=response_id,
                source_url=source_url,
                parsed=ParsedPage(
                    page_type="diagram",
                    vehicle=VehicleRecord(
                        catalog_brand="TEST",
                        model_raw=f"MODEL-{prefix}",
                        vehicle_external_id=prefix,
                    ),
                    taxonomies=[TaxonomyRecord(("ENGINE",))],
                    diagrams=[DiagramRecord("D-1", "ENGINE", category_path=("ENGINE",))],
                    parts=[
                        PartRecord(
                            number_raw=f"PART-{prefix}",
                            name_en_raw="TEST PART",
                            diagram_code_raw="D-1",
                        )
                    ],
                ),
                verified_fitments=False,
                fitment_derivation="station_test_archive_fitment",
            )
            vehicle_cursor = await repository.connection.execute(
                "SELECT id FROM vehicle_configurations WHERE source_url = ?", (source_url,)
            )
            vehicle = await vehicle_cursor.fetchone()
            assert vehicle is not None
            vehicle_id = int(vehicle["id"])

            async with repository.transaction() as connection:
                await connection.execute(
                    "DELETE FROM vin_decode_requests WHERE vin = ?", (TEST_VIN,)
                )
                mapping_cursor = await connection.execute(
                    "SELECT id, response_id FROM vin_vehicle_mappings WHERE vin = ?", (TEST_VIN,)
                )
                existing = await mapping_cursor.fetchone()
                if existing is not None:
                    await connection.execute(
                        "DELETE FROM reconciliation_cases "
                        "WHERE subject_type = 'vin_vehicle_mapping' AND subject_key = ?",
                        (str(existing["id"]),),
                    )
                    await connection.execute(
                        "DELETE FROM vin_vehicle_mappings WHERE id = ?", (existing["id"],)
                    )
                    if existing["response_id"] is not None:
                        await connection.execute(
                            "DELETE FROM vin_decode_responses WHERE id = ?",
                            (existing["response_id"],),
                        )
                await connection.execute(
                    """
                    INSERT INTO vin_decode_requests(
                        vin, status, requested_by, created_at, updated_at
                    ) VALUES (?, 'pending', 'integration-test', UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))
                    """,
                    (TEST_VIN,),
                )

            service = StationCatalogService(
                repository,
                NhtsaConfig(),
                decoder=decoder,
                worker_id="station-integration-test",
            )
            first = await service.run(run_key="station-integration-test")
            async with repository.transaction() as connection:
                await connection.execute(
                    "UPDATE vin_vehicle_mappings "
                    "SET partsouq_vehicle_configuration_id = ? WHERE vin = ?",
                    (vehicle_id, TEST_VIN),
                )
            second = await service.run(run_key="station-integration-test-linked")
            third = await service.run(run_key="station-integration-test-rerun")

            assert first["status"] == "completed"
            assert second["status"] == "completed"
            assert third["status"] == "completed"
            assert decoder.calls == [(TEST_VIN,)]
            cursor = await repository.connection.execute(
                """
                SELECT q.status, q.attempts, vm.make_name, vm.model_name, vm.model_year,
                       vm.decode_status, vr.http_status, vr.body_sha256,
                       SHA2(vr.body_json, 256) AS actual_body_sha256
                FROM vin_decode_requests AS q
                JOIN vin_vehicle_mappings AS vm ON vm.id = q.mapping_id
                JOIN vin_decode_responses AS vr ON vr.id = vm.response_id
                WHERE q.vin = ?
                """,
                (TEST_VIN,),
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row["status"] == "completed"
            assert int(row["attempts"]) == 1
            assert row["make_name"] == "HONDA"
            assert row["model_name"] == "CIVIC"
            assert int(row["model_year"]) == 1991
            assert row["decode_status"] == "decoded"
            assert int(row["http_status"]) == 200
            assert row["body_sha256"] == row["actual_body_sha256"]

            projected = await repository.connection.execute(
                """
                SELECT vpf.id,
                       (SELECT COUNT(*) FROM record_sources AS rs
                        WHERE rs.record_type = 'vin_part_fitment'
                          AND rs.record_id = vpf.id) AS provenance_count
                FROM vin_part_fitments AS vpf
                JOIN vin_vehicle_mappings AS vm ON vm.id = vpf.vin_vehicle_mapping_id
                WHERE vm.vin = ?
                """,
                (TEST_VIN,),
            )
            projected_row = await projected.fetchone()
            assert projected_row is not None
            assert int(projected_row["provenance_count"]) >= 1
            assert int(second["vin_fitments_touched"]) >= 1
            assert int(third["vin_fitments_touched"]) == 0

            async with repository.transaction() as connection:
                await connection.execute(
                    "UPDATE vin_vehicle_mappings "
                    "SET partsouq_vehicle_configuration_id = NULL WHERE vin = ?",
                    (TEST_VIN,),
                )
            unlinked = await service.run(run_key="station-integration-test-unlinked")
            assert int(unlinked["vin_fitments_touched"]) >= 1
            remaining = await repository._scalar(  # noqa: SLF001 - integration invariant
                """
                SELECT COUNT(*)
                FROM vin_part_fitments AS vpf
                JOIN vin_vehicle_mappings AS vm ON vm.id = vpf.vin_vehicle_mapping_id
                WHERE vm.vin = ?
                """,
                (TEST_VIN,),
            )
            assert remaining == 0
        finally:
            async with repository.transaction() as connection:
                mapping_cursor = await connection.execute(
                    "SELECT id, response_id FROM vin_vehicle_mappings WHERE vin = ?", (TEST_VIN,)
                )
                mapping = await mapping_cursor.fetchone()
                await connection.execute(
                    "DELETE FROM vin_decode_requests WHERE vin = ?", (TEST_VIN,)
                )
                if mapping is not None:
                    await connection.execute(
                        "DELETE rs FROM record_sources AS rs "
                        "JOIN vin_part_fitments AS vpf ON rs.record_type = 'vin_part_fitment' "
                        "AND rs.record_id = vpf.id "
                        "WHERE vpf.vin_vehicle_mapping_id = ?",
                        (mapping["id"],),
                    )
                    await connection.execute(
                        "DELETE FROM vin_part_fitments WHERE vin_vehicle_mapping_id = ?",
                        (mapping["id"],),
                    )
                    await connection.execute(
                        "DELETE FROM reconciliation_cases "
                        "WHERE subject_type = 'vin_vehicle_mapping' AND subject_key = ?",
                        (str(mapping["id"]),),
                    )
                    await connection.execute(
                        "DELETE FROM vin_vehicle_mappings WHERE id = ?", (mapping["id"],)
                    )
                    if mapping["response_id"] is not None:
                        await connection.execute(
                            "DELETE FROM vin_decode_responses WHERE id = ?",
                            (mapping["response_id"],),
                        )
            await repository.close()

    asyncio.run(scenario())
