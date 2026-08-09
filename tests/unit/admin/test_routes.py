from __future__ import annotations

import re

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
