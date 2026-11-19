"""Mock ShopHub store API (R0). Plain sync FastAPI + stdlib sqlite3.

Run:  uvicorn --factory store.app:create_app --port 8001
The merchant always comes from the bearer token, never from the request.
"""
import logging
import os
import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from store.seed import SEED_VERSION, seed_database

log = logging.getLogger("store")

OrderStatus = Literal["PLACED", "PAID", "SHIPPED", "DELIVERED", "CANCELLED", "REFUNDED"]

# action -> (statuses it is allowed from, status after)
TRANSITIONS = {
    "refund": (("PAID", "SHIPPED", "DELIVERED"), "REFUNDED"),
    "cancel": (("PLACED", "PAID"), "CANCELLED"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS merchants (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, merchant TEXT REFERENCES merchants(id));
CREATE TABLE IF NOT EXISTS tokens (
    token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id));
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY, merchant TEXT NOT NULL REFERENCES merchants(id),
    customer_name TEXT, customer_email TEXT, customer_phone TEXT,
    status TEXT NOT NULL, total REAL NOT NULL, placed_at TEXT NOT NULL, customer_note TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 5000")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def init_db(path: str, reseed: bool) -> None:
    """Create tables, then seed once. Only reseed=True regenerates existing data."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = connect(path)
    try:
        db.executescript(SCHEMA)
        if reseed:
            for table in ("tokens", "users", "orders", "merchants", "meta"):
                db.execute(f"DELETE FROM {table}")
            seed_database(db)
        elif db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0:
            seed_database(db)
        else:
            row = db.execute("SELECT value FROM meta WHERE key = 'seed_version'").fetchone()
            if row is None or row["value"] != SEED_VERSION:
                log.warning("seed version changed but STORE_RESEED is false: keeping existing data")
        db.commit()
    finally:
        db.close()


class OrderId(BaseModel):
    id: int


def create_app(db_path: str | None = None, reseed: bool | None = None) -> FastAPI:
    path = str(db_path or os.environ.get("STORE_DB_PATH", "./data/store.db"))
    if reseed is None:
        reseed = os.environ.get("STORE_RESEED", "false").lower() == "true"
    init_db(path, reseed)

    app = FastAPI(title="ShopHub mock store")

    def get_db():
        db = connect(path)
        try:
            yield db
        finally:
            db.close()

    def get_caller(authorization: str | None = Header(default=None), db: sqlite3.Connection = Depends(get_db)):
        """Token -> {user, merchant}. Missing or unknown token: 401. Merchant may be None."""
        scheme, _, token = (authorization or "").partition(" ")
        row = None
        if scheme == "Bearer" and token:
            row = db.execute(
                "SELECT users.name AS user, users.merchant AS merchant "
                "FROM tokens JOIN users ON users.id = tokens.user_id WHERE tokens.token = ?",
                (token,),
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=401, detail="Unauthorized")
        return {"user": row["user"], "merchant": row["merchant"]}

    def get_merchant(caller: dict = Depends(get_caller)) -> str:
        """For order endpoints: a token with no merchant is rejected like a bad token."""
        if caller["merchant"] is None:
            raise HTTPException(status_code=401, detail="Unauthorized")
        return caller["merchant"]

    def find_order(db: sqlite3.Connection, merchant: str, order_id: int) -> dict | None:
        """Unknown and foreign ids both give None: the caller cannot tell them apart."""
        row = db.execute("SELECT * FROM orders WHERE id = ? AND merchant = ?", (order_id, merchant)).fetchone()
        return dict(row) if row else None

    def change_status(db: sqlite3.Connection, merchant: str, order_id: int, action: str) -> dict:
        allowed_from, new_status = TRANSITIONS[action]
        if find_order(db, merchant, order_id) is None:
            raise HTTPException(status_code=404, detail="Order not found")
        marks = ",".join("?" * len(allowed_from))
        changed = db.execute(
            f"UPDATE orders SET status = ? WHERE id = ? AND merchant = ? AND status IN ({marks})",
            (new_status, order_id, merchant, *allowed_from),
        ).rowcount
        db.commit()
        order = find_order(db, merchant, order_id)
        if changed == 0:
            raise HTTPException(
                status_code=409,
                detail={"message": "Invalid state transition", "current_status": order["status"]},
            )
        return order

    @app.get("/auth/me")
    def auth_me(caller: dict = Depends(get_caller)):
        return caller

    @app.get("/orders")
    def list_orders(status: OrderStatus | None = None, merchant: str = Depends(get_merchant),
                    db: sqlite3.Connection = Depends(get_db)):
        if status:
            rows = db.execute("SELECT * FROM orders WHERE merchant = ? AND status = ? ORDER BY id", (merchant, status))
        else:
            rows = db.execute("SELECT * FROM orders WHERE merchant = ? ORDER BY id", (merchant,))
        return [dict(r) for r in rows]

    @app.get("/orders/lookup")
    def lookup_order(id: int, merchant: str = Depends(get_merchant), db: sqlite3.Connection = Depends(get_db)):
        # Legacy behaviour on purpose: always HTTP 200, and a miss is {"status": false}.
        # A found order also has a "status" key (a string), so clients must test `is False`.
        order = find_order(db, merchant, id)
        return order if order else {"status": False, "message": "Order not found"}

    @app.post("/orders/refund")
    def refund_order(body: OrderId, merchant: str = Depends(get_merchant), db: sqlite3.Connection = Depends(get_db)):
        return change_status(db, merchant, body.id, "refund")

    @app.post("/orders/cancel")
    def cancel_order(body: OrderId, merchant: str = Depends(get_merchant), db: sqlite3.Connection = Depends(get_db)):
        return change_status(db, merchant, body.id, "cancel")

    return app
