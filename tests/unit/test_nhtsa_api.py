from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest

from partsouq_crawler.nhtsa.api import (
    NhtsaApiParser,
    NhtsaApiPolicy,
    NhtsaApiPolicyError,
)
from partsouq_crawler.nhtsa.api_client import (
    NhtsaApiError,
    nhtsa_transient_retry_delay,
)
from partsouq_crawler.nhtsa.api_service import NhtsaApiSyncService
from partsouq_crawler.nhtsa.config import NhtsaConfig
from partsouq_crawler.nhtsa.datasets import ApiSource
from partsouq_crawler.nhtsa.models import DownloadedArtifact
from partsouq_crawler.nhtsa.repository import NhtsaMySQLRepository


def test_api_policy_allows_only_declared_non_vin_collection_endpoints() -> None:
    policy = NhtsaApiPolicy()
    policy.validate("https://vpic.nhtsa.dot.gov/api/vehicles/GetAllMakes?format=json")
    policy.validate(
        "https://vpic.nhtsa.dot.gov/api/vehicles/GetAllManufacturers?format=json&page=2"
    )
    policy.validate("https://api.nhtsa.gov/CSSIStation/state/NV?format=json")

    forbidden = (
        "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVin/123?format=json",
        "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVINValuesBatch/?format=json",
        "https://vpic.nhtsa.dot.gov/api/vehicles/GetAllMakes?format=json&page=2",
        "https://api.nhtsa.gov/recalls/recallsByVehicle?format=json",
        "https://example.com/api/vehicles/GetAllMakes?format=json",
    )
    for url in forbidden:
        with pytest.raises(NhtsaApiPolicyError):
            policy.validate(url)


def test_api_parser_preserves_nested_payload_and_provenance() -> None:
    source = ApiSource(
        key="vpic_manufacturers_page_001",
        dataset_name="vpic_manufacturers",
        url=("https://vpic.nhtsa.dot.gov/api/vehicles/GetAllManufacturers?format=json&page=1"),
    )
    body = json.dumps(
        {
            "Count": 1,
            "Message": "Response returned successfully",
            "Results": [
                {
                    "Country": "UNITED STATES (USA)",
                    "Mfr_CommonName": "Tesla",
                    "Mfr_ID": 955,
                    "Mfr_Name": "TESLA, INC.",
                    "VehicleTypes": [{"IsPrimary": True, "Name": "Passenger Car"}],
                }
            ],
        }
    ).encode()

    document = NhtsaApiParser().parse(body, source)
    assert document.count == 1
    assert document.rejections == ()
    record = document.records[0]
    assert record.external_id == "955"
    assert record.make_name == "Tesla"
    assert record.source_line == 1
    assert json.loads(record.payload_json)["VehicleTypes"][0]["IsPrimary"] is True
    assert "VehicleTypes" in document.member.field_names


def test_variable_value_context_is_part_of_natural_key() -> None:
    source = ApiSource(
        key="vpic_variable_5_values",
        dataset_name="vpic_variable_values",
        url=("https://vpic.nhtsa.dot.gov/api/vehicles/GetVehicleVariableValuesList/5?format=json"),
        context=(("Variable_ID", "5"),),
    )
    body = json.dumps(
        {
            "Count": 1,
            "Message": "Results returned successfully",
            "Results": [{"ElementName": "Body Class", "Id": 1, "Name": "Convertible"}],
        }
    ).encode()

    record = NhtsaApiParser().parse(body, source).records[0]
    assert record.natural_key_text == "5\x1f1"
    assert json.loads(record.payload_json)["Variable_ID"] == "5"


def test_cssi_identity_preserves_same_station_rows_with_distinct_email() -> None:
    source = ApiSource(
        key="cssi_state_co",
        dataset_name="cssi_stations",
        url="https://api.nhtsa.gov/CSSIStation/state/CO?format=json",
    )
    common = {
        "Organization": "Colorado State University PD",
        "AddressLine1": "750 Meridian Ave",
        "City": "Fort Collins",
        "State": "CO",
        "Zip": "80523",
        "Phone1": "970-657-4823",
        "ContactFirstName": "Ashleigh",
        "ContactLastName": "Rose",
        "LocationLatitude": 40.569126,
        "LocationLongitude": -105.079308,
    }
    body = json.dumps(
        {
            "Count": 2,
            "Message": "Results returned successfully",
            "Results": [
                {**common, "Email": None},
                {**common, "Email": "ashleigh.rose@colostate.edu"},
            ],
        }
    ).encode()

    records = NhtsaApiParser().parse(body, source).records

    assert len(records) == 2
    assert records[0].natural_key_sha256 != records[1].natural_key_sha256


def test_nhtsa_transient_retry_delay_is_bounded_and_honors_retry_after() -> None:
    assert nhtsa_transient_retry_delay(1) == 30
    assert nhtsa_transient_retry_delay(2) == 60
    assert nhtsa_transient_retry_delay(3) == 120
    assert nhtsa_transient_retry_delay(1, "600") == 600


def test_api_source_retries_transient_503_without_publishing_partial_data(
    tmp_path, monkeypatch
) -> None:
    source = ApiSource(
        key="vpic_all_makes",
        dataset_name="vpic_makes",
        url="https://vpic.nhtsa.dot.gov/api/vehicles/GetAllMakes?format=json",
    )
    body_path = tmp_path / "makes.json"
    body_path.write_text(
        json.dumps(
            {
                "Count": 1,
                "Message": "Response returned successfully",
                "Results": [{"Make_ID": 1, "Make_Name": "TEST"}],
            }
        )
    )

    class FakeRepository:
        def current_artifact(self, dataset_name: str, source_key: str) -> dict[str, object]:
            assert (dataset_name, source_key) == ("vpic_makes", "vpic_all_makes")
            return {"id": 7, "stored_path": str(body_path), "source_rows": 1}

    class FlakyClient:
        calls = 0

        async def fetch(self, *_args: object, **_kwargs: object):
            self.calls += 1
            if self.calls == 1:
                raise NhtsaApiError(
                    "temporary upstream failure",
                    retryable=True,
                    status=503,
                )
            return (
                DownloadedArtifact(
                    http_status=304,
                    response_headers={},
                    path=None,
                    sha256=None,
                    byte_count=0,
                    reused_artifact_id=7,
                ),
                None,
            )

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("partsouq_crawler.nhtsa.api_service.asyncio.sleep", fake_sleep)
    client = FlakyClient()
    service = NhtsaApiSyncService(
        cast(NhtsaMySQLRepository, FakeRepository()),
        NhtsaConfig(api_delay_seconds=0),
    )

    imported = asyncio.run(service._sync_source(cast(Any, client), source))

    assert imported.artifact_id == 7
    assert imported.downloaded is False
    assert client.calls == 2
    assert service.request_count == 2
    assert sleeps == [30]
