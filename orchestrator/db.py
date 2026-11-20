"""The orchestrator's SQLite file: opened with WAL and a busy timeout, plus the thread ownership table.

The same file also holds the LangGraph checkpoints (own tables, managed by `AsyncSqliteSaver`).
Module 5 adds the `proposals` table here.
"""
import os

import aiosqlite


async def connect(path: str) -> aiosqlite.Connection:
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = await aiosqlite.connect(path)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    return conn


class ThreadBindings:
    """Which merchant owns which thread. Thread ids are made by the server, never accepted from the client."""

    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def setup(self) -> None:
        await self.conn.execute(
            "CREATE TABLE IF NOT EXISTS thread_bindings ("
            " thread_id TEXT PRIMARY KEY, merchant TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        await self.conn.commit()

    async def create(self, thread_id: str, merchant: str) -> None:
        await self.conn.execute("INSERT INTO thread_bindings (thread_id, merchant) VALUES (?, ?)", (thread_id, merchant))
        await self.conn.commit()

    async def is_owned_by(self, thread_id: str, merchant: str) -> bool:
        """False for an unknown thread and for another merchant's thread: the caller cannot tell them apart."""
        async with self.conn.execute("SELECT 1 FROM thread_bindings WHERE thread_id = ? AND merchant = ?", (thread_id, merchant)) as cur:
            return await cur.fetchone() is not None
