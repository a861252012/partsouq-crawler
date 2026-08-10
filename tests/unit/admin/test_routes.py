from __future__ import annotations

import re

import pytest

from partsouq_crawler.admin.app import create_app
from partsouq_crawler.admin.config import AdminConfig
from partsouq_crawler.admin.query_trace import QueryTrace

from .fakes import ScriptedDatabase


def test_list_route_reports_fixed_three_query_budget() -> None:
    databases: list[ScriptedDatabase] = []

    def factory(_config: AdminConfig, trace: QueryTrace) -> ScriptedDatabase:
        database = ScriptedDatabase(trace, dataset_size=10_000)
        databases.append(database)
        return database

    app = create_app(
        AdminConfig(secret_key="test-secret", page_size=25),
        database_factory=factory,
    )
    app.testing = True

    response = app.test_client().get("/entities/part_numbers")

    assert response.status_code == 200
    assert response.headers["X-Admin-Query-Count"] == "3"
    assert response.headers["X-Admin-Query-Tags"].split(",") == [
        "list.keys.part_numbers",
        "list.source-batch.part_numbers",
        "list.manual-batch.part_numbers",
    ]
    assert databases[-1].closed


def test_create_requires_csrf_and_appends_audit_event() -> None:
    databases: list[ScriptedDatabase] = []

    def factory(_config: AdminConfig, trace: QueryTrace) -> ScriptedDatabase:
        database = ScriptedDatabase(trace)
        databases.append(database)
        return database

    app = create_app(
        AdminConfig(secret_key="test-secret"),
        database_factory=factory,
    )
    app.testing = True
    client = app.test_client()

    rejected = client.post(
        "/entities/part_numbers/new",
        data={"payload_json": '{"number_raw":"P-1"}', "actor": "tester", "reason": "test"},
    )
    assert rejected.status_code == 400
    assert not databases[-1].calls

    form = client.get("/entities/part_numbers/new")
    token_match = re.search(rb'name="csrf_token" value="([^"]+)"', form.data)
    assert token_match is not None
    token = token_match.group(1).decode()
    created = client.post(
        "/entities/part_numbers/new",
        data={
            "csrf_token": token,
            "payload_json": '{"number_raw":"P-1","number_normalized":"P1"}',
            "actor": "tester",
            "reason": "test fixture",
        },
    )

    assert created.status_code == 302
    assert created.headers["X-Admin-Query-Count"] == "2"
    assert [call.tag for call in databases[-1].calls] == [
        "write.create-head.part_numbers",
        "write.append-event.part_numbers",
    ]


def test_typed_form_and_vin_queue_do_not_require_raw_json() -> None:
    databases: list[ScriptedDatabase] = []

    def factory(_config: AdminConfig, trace: QueryTrace) -> ScriptedDatabase:
        database = ScriptedDatabase(trace)
        databases.append(database)
        return database

    app = create_app(AdminConfig(secret_key="test-secret"), database_factory=factory)
    app.testing = True
    client = app.test_client()
    form = client.get("/entities/part_term_mappings/new")
    token_match = re.search(rb'name="csrf_token" value="([^"]+)"', form.data)
    assert token_match is not None
    token = token_match.group(1).decode()

    created = client.post(
        "/entities/part_term_mappings/new",
        data={
            "csrf_token": token,
            "field__name_en_raw": "Brake pad",
            "field__name_en_normalized": "brake pad",
            "field__name_zh_tw": "煞車來令片",
            "field__common_names_zh_tw": '["煞車皮"]',
            "field__mapping_status": "verified",
            "actor": "tester",
            "reason": "typed form",
        },
    )
    assert created.status_code == 302
    assert [call.tag for call in databases[-1].calls] == [
        "write.create-head.part_term_mappings",
        "write.append-event.part_term_mappings",
    ]

    queued = client.post(
        "/station/vins/request",
        data={
            "csrf_token": token,
            "vin": "TEST0000000000000",
            "actor": "tester",
        },
    )
    assert queued.status_code == 302
    assert [call.tag for call in databases[-1].calls] == ["write.request-vin-decode"]

    invalid = client.post(
        "/station/vins/request",
        data={"csrf_token": token, "vin": "INVALID", "actor": "tester"},
    )
    assert invalid.status_code == 400
    assert not databases[-1].calls

    invalid_mapping = client.post(
        "/entities/part_term_mappings/new",
        data={
            "csrf_token": token,
            "field__name_en_raw": "Brake pad",
            "field__name_en_normalized": "brake pad",
            "field__common_names_zh_tw": '{"wrong":"shape"}',
            "field__confidence": "1.5",
            "actor": "tester",
            "reason": "invalid typed form",
        },
    )
    assert invalid_mapping.status_code == 400
    assert not databases[-1].calls


def test_remote_admin_requires_login_and_stable_https_configuration() -> None:
    databases: list[ScriptedDatabase] = []

    def factory(_config: AdminConfig, trace: QueryTrace) -> ScriptedDatabase:
        database = ScriptedDatabase(trace)
        databases.append(database)
        return database

    with pytest.raises(ValueError, match="USERNAME/PASSWORD"):
        AdminConfig(
            bind_host="0.0.0.0", secret_key="stable", secure_cookie=True
        ).validate_server_mode()

    config = AdminConfig(
        secret_key="stable-secret",
        username="station",
        password="correct-horse",
    )
    app = create_app(config, database_factory=factory)
    app.testing = True
    client = app.test_client()

    protected = client.get("/")
    assert protected.status_code == 302
    assert "/login" in protected.headers["Location"]
    assert not databases

    login_form = client.get("/login")
    token_match = re.search(rb'name="csrf_token" value="([^"]+)"', login_form.data)
    assert token_match is not None
    token = token_match.group(1).decode()
    rejected = client.post(
        "/login",
        data={
            "csrf_token": token,
            "username": "站方",
            "password": "錯誤密碼",
        },
    )
    assert rejected.status_code == 200
    assert "帳號或密碼錯誤".encode() in rejected.data
    authenticated = client.post(
        "/login",
        data={
            "csrf_token": token,
            "username": "station",
            "password": "correct-horse",
            "next": r"/\evil.example",
        },
    )
    assert authenticated.status_code == 302
    assert authenticated.headers["Location"] == "/"
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert databases[-1].closed


def test_authenticated_admin_requires_complete_stable_configuration() -> None:
    with pytest.raises(ValueError, match="must be set together"):
        create_app(AdminConfig(username="station", secret_key="stable"))
    with pytest.raises(ValueError, match="SECRET_KEY"):
        create_app(AdminConfig(username="station", password="correct-horse"))
