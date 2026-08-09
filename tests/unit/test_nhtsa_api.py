from __future__ import annotations

import json

import pytest

from partsouq_crawler.nhtsa.api import (
    NhtsaApiParser,
    NhtsaApiPolicy,
    NhtsaApiPolicyError,
)
from partsouq_crawler.nhtsa.datasets import ApiSource


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
