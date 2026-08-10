from __future__ import annotations

import asyncio
import re
import warnings
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import aiomysql  # type: ignore[import-untyped]
from pymysql.err import OperationalError
from pymysql.err import Warning as MySQLWarning

from partsouq_crawler.config import PartSouqMySQLConfig
from partsouq_crawler.db.protocols import AsyncConnection, DatabaseRow

_NULL_SAFE_PARAMETER = re.compile(r"\bIS\s+\?", re.IGNORECASE)
_EXPECTED_MYSQL_WARNING = re.compile(
    r"(?:Duplicate entry .* for key .*|Table .* already exists|"
    r"Integer display width is deprecated.*|'VALUES function' is deprecated.*)"
)


def mysql_sql(query: str) -> str:
    """Translate the small SQLite-compatible SQL subset used by the crawler."""
    query = re.sub(r"\bINSERT\s+OR\s+IGNORE\b", "INSERT IGNORE", query, flags=re.IGNORECASE)
    query = _NULL_SAFE_PARAMETER.sub("<=> ?", query)
    return query.replace("?", "%s")


class BufferedCursor:
    def __init__(
        self,
        rows: Sequence[DatabaseRow],
        *,
        rowcount: int,
        lastrowid: int | None,
    ) -> None:
        self._rows = list(rows)
        self._offset = 0
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    async def fetchone(self) -> DatabaseRow | None:
        if self._offset >= len(self._rows):
            return None
        row = self._rows[self._offset]
        self._offset += 1
        return row

    async def fetchall(self) -> Sequence[DatabaseRow]:
        rows = self._rows[self._offset :]
        self._offset = len(self._rows)
        return rows


class CrawlerDictCursor(aiomysql.DictCursor):  # type: ignore[misc]
    async def _show_warnings(self, connection: aiomysql.Connection) -> None:
        result = getattr(self, "_result", None)
        if result is not None and result.has_next:
            return
        mysql_warnings = await connection.show_warnings()
        if mysql_warnings is None:
            return
        for warning_row in mysql_warnings:
            message = str(warning_row[-1])
            if _EXPECTED_MYSQL_WARNING.fullmatch(message):
                continue
            warnings.warn(message, MySQLWarning, stacklevel=4)


class MySQLConnection:
    def __init__(self, connection: aiomysql.Connection) -> None:
        self._connection = connection

    async def execute(self, query: str, parameters: Sequence[object] = ()) -> BufferedCursor:
        async with self._connection.cursor(CrawlerDictCursor) as cursor:
            await cursor.execute(mysql_sql(query), tuple(parameters))
            rows = await cursor.fetchall() if cursor.description else ()
            return BufferedCursor(
                rows,
                rowcount=cursor.rowcount,
                lastrowid=int(cursor.lastrowid) if cursor.lastrowid else None,
            )

    async def executemany(
        self, query: str, parameters: Sequence[Sequence[object]]
    ) -> BufferedCursor:
        async with self._connection.cursor(CrawlerDictCursor) as cursor:
            await cursor.executemany(mysql_sql(query), [tuple(row) for row in parameters])
            rows = await cursor.fetchall() if cursor.description else ()
            return BufferedCursor(
                rows,
                rowcount=cursor.rowcount,
                lastrowid=int(cursor.lastrowid) if cursor.lastrowid else None,
            )

    async def commit(self) -> None:
        await self._connection.commit()

    async def rollback(self) -> None:
        await self._connection.rollback()


class MySQLPoolConnection:
    def __init__(self, pool: aiomysql.Pool, *, label: str) -> None:
        self._pool = pool
        self.label = label

    async def execute(self, query: str, parameters: Sequence[object] = ()) -> BufferedCursor:
        async with self._pool.acquire() as raw:
            return await MySQLConnection(raw).execute(query, parameters)

    async def executemany(
        self, query: str, parameters: Sequence[Sequence[object]]
    ) -> BufferedCursor:
        async with self._pool.acquire() as raw:
            return await MySQLConnection(raw).executemany(query, parameters)

    async def commit(self) -> None:
        # Pool-level statements run with autocommit enabled.
        return None

    async def rollback(self) -> None:
        return None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncConnection]:
        async with self._pool.acquire() as raw:
            await raw.begin()
            connection = MySQLConnection(raw)
            try:
                yield connection
            except BaseException:
                await asyncio.shield(raw.rollback())
                raise
            else:
                await raw.commit()

    async def close(self) -> None:
        self._pool.close()
        await self._pool.wait_closed()


async def create_mysql_connection(config: PartSouqMySQLConfig) -> MySQLPoolConnection:
    pool = await aiomysql.create_pool(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        db=config.database,
        charset="utf8mb4",
        autocommit=True,
        minsize=config.pool_min_size,
        maxsize=config.pool_max_size,
        connect_timeout=config.connect_timeout_seconds,
        init_command=("SET SESSION time_zone = '+00:00', transaction_isolation = 'READ-COMMITTED'"),
    )
    connection = MySQLPoolConnection(pool, label=config.public_dsn())
    await apply_mysql_schema(connection)
    return connection


async def apply_mysql_schema(connection: MySQLPoolConnection) -> None:
    schema = Path(__file__).with_name("mysql_schema.sql").read_text(encoding="utf-8")
    statements = _schema_statements(schema)
    if not statements:
        raise RuntimeError("MySQL schema is empty")
    await connection.execute(statements[0])
    cursor = await connection.execute("SELECT MAX(version) AS version FROM schema_migrations")
    row = await cursor.fetchone()
    current_version = int(row["version"] or 0) if row is not None else 0
    if current_version < 1:
        for statement in statements[1:]:
            await connection.execute(statement)
        current_version = 1

    admin_schema_path = Path(__file__).parent.parent / "admin" / "mysql_schema.sql"
    admin_schema = admin_schema_path.read_text(encoding="utf-8")
    for statement in _schema_statements(admin_schema):
        await connection.execute(statement)

    migration_dir = Path(__file__).with_name("mysql_migrations")
    for migration_path in sorted(migration_dir.glob("[0-9][0-9][0-9]_*.sql")):
        version = int(migration_path.name.split("_", 1)[0])
        if version <= current_version:
            continue
        migration = migration_path.read_text(encoding="utf-8")
        for statement in _schema_statements(migration):
            try:
                await connection.execute(statement)
            except OperationalError as error:
                if error.args[0] not in {
                    1060,  # duplicate column after interrupted or bootstrap-applied migration
                    1061,  # duplicate index after interrupted DDL migration
                }:
                    raise
        await connection.execute(
            "INSERT IGNORE INTO schema_migrations(version, applied_at) "
            "VALUES (?, UTC_TIMESTAMP(6))",
            (version,),
        )
        current_version = version


def _schema_statements(schema: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    for line in schema.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        current.append(line)
        if stripped.endswith(";"):
            statements.append("\n".join(current).rstrip().removesuffix(";"))
            current = []
    if current:
        statements.append("\n".join(current))
    return statements
