import asyncio
import json

from store.seed import ORDERS
from tests.conftest import TOKEN_A, TOKEN_B, TOKEN_NOBODY

A_IDS = {o["id"] for o in ORDERS if o["merchant"] == "merchant_a"}
B_IDS = {o["id"] for o in ORDERS if o["merchant"] == "merchant_b"}
OWN = {TOKEN_A: A_IDS, TOKEN_B: B_IDS}
UNKNOWN = 9999


async def test_tenant_isolation(agent):
    """merchant token x skill x order id. Another merchant's order and an unknown order must look the same."""
    for token, own in OWN.items():
        for order_id in sorted(A_IDS | B_IDS | {UNKNOWN}):
            for skill in ("get_order", "get_order_note"):
                result = await agent.call(skill, {"order_id": order_id}, token=token)
                if order_id in own:
                    assert result["ok"] is True, (token, skill, order_id)
                else:
                    assert result == {"ok": False, "code": "not_found"}, (token, skill, order_id)
            if order_id not in own:  # writes on foreign or unknown orders do nothing and look the same
                for skill in ("refund_order", "cancel_order"):
                    result = await agent.call(skill, {"order_id": order_id}, token=token)
                    assert result["code"] == "NOT_FOUND_OR_NOT_ACCESSIBLE" and result["ok"] is False, (token, skill, order_id)
    for order in ORDERS:  # no foreign write changed anything
        assert await agent.order_status(order["id"], TOKEN_A if order["merchant"] == "merchant_a" else TOKEN_B) == order["status"]


async def test_lists_and_searches_are_scoped(agent):
    for token, own in OWN.items():
        listed = {o["id"] for o in (await agent.call("list_orders", token=token))["orders"]}
        assert listed == own
        for status in ("PLACED", "PAID", "SHIPPED", "DELIVERED", "CANCELLED", "REFUNDED"):
            out = await agent.call("list_orders", {"status": status}, token=token)
            assert {o["id"] for o in out["orders"]} <= own
        found = await agent.call("find_orders_by_customer_name", {"name": "maria"}, token=token)
        assert {o["id"] for m in found["matches"] for o in m["orders"]} <= own
    b_marias = await agent.call("find_orders_by_customer_name", {"name": "maria"}, token=TOKEN_B)
    assert [o["id"] for m in b_marias["matches"] for o in m["orders"]] == [2003]  # B's Maria only, so no label needed
    assert b_marias["matches"][0]["customer_label"] is None


async def test_concurrent_tokens_no_crosstalk(agent):
    async def listing(token):
        return token, {o["id"] for o in (await agent.call("list_orders", token=token))["orders"]}

    async def detail(token, order_id):
        return token, (await agent.call("get_order", {"order_id": order_id}, token=token))

    tokens = [TOKEN_A, TOKEN_B] * 15
    results = await asyncio.gather(*(listing(t) for t in tokens))
    assert all(ids == OWN[token] for token, ids in results)
    mixed = await asyncio.gather(*(detail(t, 1002 if t == TOKEN_A else 2001) for t in tokens))
    assert all(r["ok"] and r["order"]["id"] == (1002 if t == TOKEN_A else 2001) for t, r in mixed)
    crossed = await asyncio.gather(*(detail(t, 2001 if t == TOKEN_A else 1002) for t in tokens))
    assert all(r == {"ok": False, "code": "not_found"} for _, r in crossed)


async def test_fail_closed_null_merchant(agent):
    for skill, args in (("get_order", {"order_id": 1002}), ("list_orders", {}), ("refund_order", {"order_id": 1002}),
                        ("find_orders_by_customer_name", {"name": "maria"})):
        resp = await agent.rpc(skill, args, token=TOKEN_NOBODY)
        assert resp.status_code == 401 and resp.json() == {"detail": "Unauthorized"}
        assert "result" not in resp.json()
    assert await agent.order_status(1002) == "PAID"
