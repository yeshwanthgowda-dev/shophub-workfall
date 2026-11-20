"""The proposals table. A proposal is a refund or cancel waiting for a human to approve it.

Every state change is one atomic UPDATE with a status check, so two approvals can never both win.
Times are stored as fixed-format UTC strings, so comparing them as text compares them as times.
No token is ever stored here.
"""
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import aiosqlite

TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(TIME_FORMAT)


class Proposals:
    def __init__(self, conn: aiosqlite.Connection, clock, ttl_seconds: int):
        self.conn, self.clock, self.ttl_seconds = conn, clock, ttl_seconds
        conn.row_factory = aiosqlite.Row

    async def setup(self) -> None:
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS proposals (
                id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
                merchant TEXT NOT NULL, order_id INTEGER NOT NULL, action TEXT NOT NULL,
                amount REAL NOT NULL, from_status TEXT NOT NULL, to_status TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', outcome TEXT, current_status TEXT,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL, note_flagged INTEGER NOT NULL DEFAULT 0
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_proposal_per_tool_call ON proposals (thread_id, tool_call_id);
            CREATE UNIQUE INDEX IF NOT EXISTS one_pending_per_order ON proposals (merchant, order_id) WHERE status = 'pending';
            """
        )
        await self.conn.commit()

    async def get(self, proposal_id: str) -> dict | None:
        async with self.conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def create(self, *, thread_id: str, tool_call_id: str, merchant: str, order_id: int, action: str,
                     amount: float, from_status: str, to_status: str, note_flagged: bool) -> tuple[bool, dict]:
        """(True, row) when the row exists for this tool call (new, or already made: retries are safe).
        (False, row_of_the_other_pending_proposal) when this order already has a pending proposal."""
        async with self.conn.execute("SELECT id FROM proposals WHERE thread_id = ? AND tool_call_id = ?", (thread_id, tool_call_id)) as cur:
            same_call = await cur.fetchone()
        if same_call:
            return True, await self.get(same_call["id"])
        now = self.clock()
        # An abandoned, expired proposal must not hold the one-pending-per-order slot.
        await self.conn.execute(
            "UPDATE proposals SET status = 'expired' WHERE merchant = ? AND order_id = ? AND status = 'pending' AND expires_at < ?",
            (merchant, order_id, stamp(now)),
        )
        proposal_id = str(uuid.uuid4())
        try:
            await self.conn.execute(
                "INSERT INTO proposals (id, thread_id, tool_call_id, merchant, order_id, action, amount, from_status, to_status,"
                " created_at, expires_at, note_flagged) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (proposal_id, thread_id, tool_call_id, merchant, order_id, action, amount, from_status, to_status,
                 stamp(now), stamp(now + timedelta(seconds=self.ttl_seconds)), int(note_flagged)),
            )
        except sqlite3.IntegrityError:
            await self.conn.rollback()
            async with self.conn.execute("SELECT id FROM proposals WHERE merchant = ? AND order_id = ? AND status = 'pending'", (merchant, order_id)) as cur:
                other = await cur.fetchone()
            return False, await self.get(other["id"])
        await self.conn.commit()
        return True, await self.get(proposal_id)

    async def claim(self, proposal_id: str, new_status: str) -> bool:
        """pending and not expired -> new_status. True for exactly one caller."""
        cur = await self.conn.execute(
            "UPDATE proposals SET status = ? WHERE id = ? AND status = 'pending' AND expires_at > ?",
            (new_status, proposal_id, stamp(self.clock())),
        )
        await self.conn.commit()
        return cur.rowcount == 1

    async def why_not_claimable(self, proposal_id: str) -> str:
        """The 409 code after a failed claim."""
        row = await self.get(proposal_id)
        if row["status"] == "pending":  # still marked pending, so it must be past its expiry time
            await self.conn.execute("UPDATE proposals SET status = 'expired' WHERE id = ? AND status = 'pending'", (proposal_id,))
            await self.conn.commit()
            return "expired"
        if row["status"] in ("expired", "rejected"):
            return row["status"]
        return "already_used"  # approved, executed or failed

    async def finish(self, proposal_id: str, status: str, outcome: str, current_status: str | None = None) -> None:
        await self.conn.execute(
            "UPDATE proposals SET status = ?, outcome = ?, current_status = ? WHERE id = ? AND status = 'approved'",
            (status, outcome, current_status, proposal_id),
        )
        await self.conn.commit()

    async def supersede_pending(self, thread_id: str) -> None:
        """A new user message replaces whatever was waiting for approval on this thread."""
        await self.conn.execute(
            "UPDATE proposals SET status = 'rejected', outcome = 'SUPERSEDED' WHERE thread_id = ? AND status = 'pending'", (thread_id,))
        await self.conn.commit()
