from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

DatabaseRow = Mapping[str, Any]


class AsyncCursor(Protocol):
    rowcount: int
    lastrowid: int | None

    async def fetchone(self) -> DatabaseRow | None: ...

    async def fetchall(self) -> Sequence[DatabaseRow]: ...


class AsyncConnection(Protocol):
    async def execute(self, query: str, parameters: Sequence[object] = ()) -> AsyncCursor: ...

    async def executemany(
        self, query: str, parameters: Sequence[Sequence[object]]
    ) -> AsyncCursor: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class TransactionProvider(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[AsyncConnection]: ...

    async def close(self) -> None: ...


AsyncTransaction = AsyncIterator[AsyncConnection]
