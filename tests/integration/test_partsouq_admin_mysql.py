from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterator

import pymysql
import pytest
from flask import Flask
from flask.testing import FlaskClient
from pymysql.cursors import DictCursor

from partsouq_crawler.admin.app import create_app
from partsouq_crawler.admin.config import AdminConfig

pytestmark = pytest.mark.skipif(
    os.getenv("PARTSOUQ_TEST_MYSQL") != "1",
    reason="set PARTSOUQ_TEST_MYSQL=1 to run local MySQL integration tests",
)


def _connection(*, admin: bool = False) -> pymysql.Connection[DictCursor]:
    return pymysql.connect(
        host=os.getenv("PARTSOUQ_MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("PARTSOUQ_MYSQL_PORT", "3308")),
        user=(
            os.getenv("PARTSOUQ_ADMIN_MYSQL_USER", "partsouq_admin")
            if admin
            else os.getenv("PARTSOUQ_MYSQL_USER", "partsouq")
        ),
        password=(
            os.getenv("PARTSOUQ_ADMIN_MYSQL_PASSWORD", "partsouq-admin-local")
            if admin
            else os.getenv("PARTSOUQ_MYSQL_PASSWORD", "partsouq-local")
        ),
        database=os.getenv("PARTSOUQ_TEST_MYSQL_DATABASE", "partsouq_test"),
        charset="utf8mb4",
        autocommit=True,
        cursorclass=DictCursor,
    )


def _source_row(connection: pymysql.Connection[DictCursor]) -> tuple[int, str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO part_numbers(
                part_brand_raw, number_raw, number_normalized, name_en_raw,
                is_assembly_inferred, assembly_inference_reason, source_url,
                created_at, updated_at
            ) VALUES (
                'TEST', 'ADMIN-E2E-001', 'ADMIN-E2E-001', 'immutable source',
                0, NULL, 'https://example.invalid/admin-e2e',
                UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
            )
            ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)
            """
        )
        source_id = int(cursor.lastrowid)
        cursor.execute("SELECT * FROM part_numbers WHERE id = %s", (source_id,))
        row = cursor.fetchone()
    assert row is not None
    encoded = json.dumps(row, sort_keys=True, default=str, separators=(",", ":")).encode()
    return source_id, hashlib.sha256(encoded).hexdigest()


def _row_sha256(connection: pymysql.Connection[DictCursor], source_id: int) -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM part_numbers WHERE id = %s", (source_id,))
        row = cursor.fetchone()
    assert row is not None
    encoded = json.dumps(row, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _csrf(client: FlaskClient) -> str:
    response = client.get("/entities/part_numbers/new")
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    assert match is not None
    return match.group(1).decode()


@pytest.fixture
def mysql_admin_app() -> Iterator[tuple[Flask, int, str]]:
    source_connection = _connection()
    source_id, source_sha256 = _source_row(source_connection)
    source_connection.close()
    config = AdminConfig(
        mysql_host=os.getenv("PARTSOUQ_MYSQL_HOST", "127.0.0.1"),
        mysql_port=int(os.getenv("PARTSOUQ_MYSQL_PORT", "3308")),
        mysql_user=os.getenv("PARTSOUQ_ADMIN_MYSQL_USER", "partsouq_admin"),
        mysql_password=os.getenv("PARTSOUQ_ADMIN_MYSQL_PASSWORD", "partsouq-admin-local"),
        mysql_database=os.getenv("PARTSOUQ_TEST_MYSQL_DATABASE", "partsouq_test"),
        secret_key="mysql-integration-secret",
        default_actor="integration-test",
        page_size=25,
    )
    app = create_app(config)
    app.testing = True
    yield app, source_id, source_sha256


def test_mysql_admin_crud_audit_permissions_and_query_contract(
    mysql_admin_app: tuple[Flask, int, str],
) -> None:
    app, source_id, source_sha256 = mysql_admin_app
    client = app.test_client()

    listing = client.get("/entities/part_numbers")
    assert listing.status_code == 200
    assert listing.headers["X-Admin-Query-Count"] == "3"

    search = client.get("/entities/part_numbers?q=ADMIN-E2E")
    assert search.status_code == 200
    assert search.headers["X-Admin-Query-Count"] == "3"
    assert b"ADMIN-E2E-001" in search.data

    token = _csrf(client)
    created = client.post(
        "/entities/part_numbers/new",
        data={
            "csrf_token": token,
            "payload_json": json.dumps(
                {
                    "part_brand_raw": "TEST",
                    "number_raw": "MANUAL-E2E-001",
                    "number_normalized": "MANUAL-E2E-001",
                }
            ),
            "actor": "integration-test",
            "reason": "verify create",
        },
    )
    assert created.status_code == 302
    identity_key = created.headers["Location"].rsplit("/", 1)[-1]

    detail = client.get(created.headers["Location"])
    assert detail.status_code == 200
    assert b"MANUAL-E2E-001" in detail.data

    updated = client.post(
        f"/entities/part_numbers/{identity_key}/update",
        data={
            "csrf_token": token,
            "revision": "1",
            "payload_json": json.dumps({"name_en_raw": "manual updated"}),
            "actor": "integration-test",
            "reason": "verify update",
        },
    )
    assert updated.status_code == 302
    stale = client.post(
        f"/entities/part_numbers/{identity_key}/update",
        data={
            "csrf_token": token,
            "revision": "1",
            "payload_json": json.dumps({"name_en_raw": "stale write"}),
            "actor": "integration-test",
            "reason": "verify conflict",
        },
    )
    assert stale.status_code == 409

    retired = client.post(
        f"/entities/part_numbers/{identity_key}/retire",
        data={
            "csrf_token": token,
            "revision": "2",
            "actor": "integration-test",
            "reason": "verify retire",
        },
    )
    assert retired.status_code == 302
    restored = client.post(
        f"/entities/part_numbers/{identity_key}/restore",
        data={
            "csrf_token": token,
            "revision": "3",
            "actor": "integration-test",
            "reason": "verify restore",
        },
    )
    assert restored.status_code == 302

    admin_connection = _connection(admin=True)
    with admin_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*) AS count
            FROM admin_override_events
            WHERE entity_type = 'part_numbers' AND identity_key = %s
            """,
            (identity_key,),
        )
        event_count = cursor.fetchone()
        assert event_count is not None
        assert int(event_count["count"]) == 4
        with pytest.raises(pymysql.MySQLError):
            cursor.execute(
                "UPDATE part_numbers SET name_en_raw = 'forbidden' WHERE id = %s",
                (source_id,),
            )
    admin_connection.close()

    source_connection = _connection()
    assert _row_sha256(source_connection, source_id) == source_sha256
    source_connection.close()
