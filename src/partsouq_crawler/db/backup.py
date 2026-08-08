from __future__ import annotations

from pathlib import Path

import aiosqlite


async def backup_database(source: aiosqlite.Connection, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = await aiosqlite.connect(destination)
    try:
        await source.backup(target)
        await target.commit()
    finally:
        await target.close()
