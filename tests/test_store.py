import logging
import sqlite3

import pytest
from fastapi.testclient import TestClient

from store.app import create_app
from store.seed import ORDERS, SEED_VERSION

A = {"Authorization": "Bearer tok_demo_merchant_a"}
B = {"Authorization": "Bearer tok_demo_merchant_b"}
NOBODY = {"Authorization": "Bearer tok_demo_nomerchant"}
HEADERS = {"merchant_a": A, "merchant_b": B}

A_IDS = {o["id"] for o in ORDERS if o["merchant"] == "merchant_a"}
B_IDS = {o["id"] for o in ORDERS if o["merchant"] == "merchant_b"}


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "store.db")


@pytest.fixture
def client(db_path):
    return TestClient(create_app(db_path))


def test_seed_fixture_shape():
    assert len(ORDERS) >= 12
    assert {o["status"] for o in ORDERS} == {"PLACED", "PAID", "SHIPPED", "DELIVERED", "CANCELLED", "REFUNDED"}
    assert all(1000 <= i < 2000 for i in A_IDS) and all(2000 <= i < 3000 for i in B_IDS)
    marias_in_a = {o["customer_name"] for o in ORDERS if o["customer_name"].startswith("Maria") and o["merchant"] == "merchant_a"}
    assert marias_in_a == {"Maria Lopez", "Maria Chen"}  # two different customers, for the label tests


def test_store_scoping(client):
    assert {o["id"] for o in client.get("/orders", headers=A).json()} == A_IDS
    assert {o["id"] for o in client.get("/orders", headers=B).json()} == B_IDS
    assert client.get("/auth/me", headers=A).json() == {"user": "alice", "merchant": "merchant_a"}
    paid = client.get("/orders?status=PAID", headers=A).json()
    assert paid and all(o["status"] == "PAID" and o["id"] in A_IDS for o in paid)
    # B's token cannot read, refund or cancel A's order
    assert client.get("/orders/lookup?id=1002", headers=B).json()["status"] is False
    assert client.post("/orders/refund", json={"id": 1002}, headers=B).status_code == 404
    assert client.post("/orders/cancel", json={"id": 1002}, headers=B).status_code == 404
    assert client.get("/orders/lookup?id=1002", headers=A).json()["status"] == "PAID"  # untouched


def test_store_legacy_envelope(client):
    hit = client.get("/orders/lookup?id=1002", headers=A)
    assert hit.status_code == 200 and hit.json()["status"] == "PAID" and hit.json()["id"] == 1002
    for order_id in (9999, 2001):  # unknown, and owned by B
        miss = client.get(f"/orders/lookup?id={order_id}", headers=A)
        assert miss.status_code == 200  # legacy: failure is still HTTP 200
        assert miss.json() == {"status": False, "message": "Order not found"}


REFUND_FROM = {"PAID", "SHIPPED", "DELIVERED"}
CANCEL_FROM = {"PLACED", "PAID"}


@pytest.mark.parametrize("action,allowed,after", [("refund", REFUND_FROM, "REFUNDED"), ("cancel", CANCEL_FROM, "CANCELLED")])
def test_store_state_transitions(db_path, action, allowed, after):
    for order in ORDERS:
        client = TestClient(create_app(db_path, reseed=True))  # fresh seed for every order
        headers = HEADERS[order["merchant"]]
        resp = client.post(f"/orders/{action}", json={"id": order["id"]}, headers=headers)
        now = client.get(f"/orders/lookup?id={order['id']}", headers=headers).json()["status"]
        if order["status"] in allowed:
            assert resp.status_code == 200 and resp.json()["status"] == after and now == after, order["id"]
        else:
            assert resp.status_code == 409, order["id"]
            assert resp.json()["detail"]["current_status"] == order["status"] and now == order["status"]


def test_store_double_refund_is_409(client):
    assert client.post("/orders/refund", json={"id": 1002}, headers=A).status_code == 200
    second = client.post("/orders/refund", json={"id": 1002}, headers=A)
    assert second.status_code == 409 and second.json()["detail"]["current_status"] == "REFUNDED"


def test_store_unknown_equals_foreign(client):
    # id 9999 does not exist, id 2001 belongs to B: A must get byte-identical answers
    for method, url, body in [
        ("get", "/orders/lookup?id={id}", None),
        ("post", "/orders/refund", {"id": "{id}"}),
        ("post", "/orders/cancel", {"id": "{id}"}),
    ]:
        results = []
        for order_id in (9999, 2001):
            if method == "get":
                resp = client.get(url.format(id=order_id), headers=A)
            else:
                resp = client.post(url, json={"id": order_id}, headers=A)
            results.append((resp.status_code, resp.content))
        assert results[0] == results[1], url


def test_store_restart_keeps_data(db_path):
    first = TestClient(create_app(db_path))
    assert first.post("/orders/refund", json={"id": 1002}, headers=A).status_code == 200
    second = TestClient(create_app(db_path))  # new app, same file
    assert second.get("/orders/lookup?id=1002", headers=A).json()["status"] == "REFUNDED"


def test_store_seed_once(db_path):
    TestClient(create_app(db_path)).post("/orders/refund", json={"id": 1002}, headers=A)
    again = TestClient(create_app(db_path))
    orders = again.get("/orders", headers=A).json()
    assert len(orders) == len(A_IDS)  # not duplicated
    assert {o["id"]: o["status"] for o in orders}[1002] == "REFUNDED"  # not overwritten


def test_store_reseed_flag(db_path):
    TestClient(create_app(db_path)).post("/orders/refund", json={"id": 1002}, headers=A)
    fresh = TestClient(create_app(db_path, reseed=True))
    assert fresh.get("/orders/lookup?id=1002", headers=A).json()["status"] == "PAID"
    assert len(fresh.get("/orders", headers=A).json()) == len(A_IDS)


def test_store_reseed_env_var(db_path, monkeypatch):
    TestClient(create_app(db_path)).post("/orders/refund", json={"id": 1002}, headers=A)
    monkeypatch.setenv("STORE_RESEED", "true")
    assert TestClient(create_app(db_path)).get("/orders/lookup?id=1002", headers=A).json()["status"] == "PAID"


def test_store_changed_seed_version_keeps_data_and_warns(db_path, caplog):
    TestClient(create_app(db_path)).post("/orders/refund", json={"id": 1002}, headers=A)
    db = sqlite3.connect(db_path)
    db.execute("UPDATE meta SET value = ? WHERE key = 'seed_version'", (SEED_VERSION + "-old",))
    db.commit()
    db.close()
    with caplog.at_level(logging.WARNING, logger="store"):
        client = TestClient(create_app(db_path))
    assert "seed version changed" in caplog.text
    assert client.get("/orders/lookup?id=1002", headers=A).json()["status"] == "REFUNDED"


def test_store_auth_rejections(client):
    paths = ["/orders", "/orders/lookup?id=1001", "/auth/me"]
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic tok_demo_merchant_a"},
                    {"Authorization": "tok_demo_merchant_a"}):
        for path in paths:
            assert client.get(path, headers=headers).status_code == 401, (headers, path)
        assert client.post("/orders/refund", json={"id": 1002}, headers=headers).status_code == 401
    # valid token with no merchant: /auth/me answers, order endpoints refuse
    me = client.get("/auth/me", headers=NOBODY)
    assert me.status_code == 200 and me.json() == {"user": "carol", "merchant": None}
    assert client.get("/orders", headers=NOBODY).status_code == 401
    assert client.get("/orders/lookup?id=1001", headers=NOBODY).status_code == 401
    assert client.post("/orders/refund", json={"id": 1002}, headers=NOBODY).status_code == 401
    assert client.post("/orders/cancel", json={"id": 1002}, headers=NOBODY).status_code == 401
