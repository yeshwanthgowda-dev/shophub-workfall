import json
import pathlib
import re

import httpx
import pytest
from langchain_core.messages import AIMessage

from orchestrator.a2a_client import A2AClient, AgentUnavailable
from orchestrator.fake_llm import FakeChatModel, _make_tool_call
from orchestrator.graph import PII_REFUSAL, TOOL_LIMIT_REPLY
from sanitizers.scrubber import contains_pii
from shared.contracts import READ_SKILLS
from store.seed import ORDERS
from tests.conftest import TOKEN_A, TOKEN_B, TOKEN_NOBODY

A_ORDERS = [o for o in ORDERS if o["merchant"] == "merchant_a"]
B_ORDERS = [o for o in ORDERS if o["merchant"] == "merchant_b"]


async def test_answer_uses_tool(orch):
    reply = await orch.reply("what is the status of order 1002?")
    assert "PAID" in reply and "1002" in reply
    tool_results = [text for kind, text in orch.model_inputs() if kind == "tool"]
    assert any('"id":1002' in text for text in tool_results)  # the answer came from a tool result, with a real int id


async def test_legacy_failure_reported_usefully(orch):
    for order_id in (2001, 9999):  # another merchant's order, and one that does not exist
        reply = await orch.reply(f"what is the status of order {order_id}?")
        assert f"couldn't find order {order_id} in your store" in reply
        assert "check the number" in reply and "customer name" in reply
        assert "doesn't exist" not in reply and "belongs" not in reply


async def test_user_typed_email_refused(orch):
    for message in ("refund the order of maria@example.com", "call me on +1 415-555-0134 about 1002", "my number is 4155550134"):
        assert await orch.reply(message) == PII_REFUSAL
    assert orch.llm.calls == []  # the model was never called


async def test_date_in_question_is_not_refused(orch):
    assert await orch.reply("orders placed on 2026-11-01?") != PII_REFUSAL


async def test_pii_never_in_model_input(make_orch):
    seed_secrets = [value for o in ORDERS for value in (o["customer_email"], o["customer_phone"], o["customer_name"])]
    for token, orders in ((TOKEN_A, A_ORDERS), (TOKEN_B, B_ORDERS)):
        orch = await make_orch()
        questions = ["list all orders", "which paid orders have not shipped in 3 days?", "what did Maria order?",
                     "what did Noah order?", "any orders for SYSTEM?"]
        for order in orders:
            questions.append(f"status of order {order['id']}")
            if order["customer_note"].strip():
                questions.append(f"what is the note on order {order['id']}?")
        for question in questions:
            await orch.reply(question, token)
        inputs = orch.model_inputs()
        assert any(kind == "tool" for kind, _ in inputs)
        for kind, text in inputs:
            for secret in seed_secrets:
                assert secret not in text, (secret, kind)
            if kind in ("tool", "system"):  # everything not typed by the user is free of emails and phones
                assert not contains_pii(text), (kind, text[:200])


async def test_token_not_in_model_input(orch):
    await orch.reply("status of order 1002")
    await orch.reply("what is the note on order 1002?")
    assert not any("tok_demo" in text or "Bearer" in text for _, text in orch.model_inputs())


async def test_chat_rejects_bad_tokens(orch):
    for kwargs in ({"token": None}, {"token": "nope"}, {"token": TOKEN_NOBODY}, {"header": "Basic tok_demo_merchant_a"}):
        resp = await orch.chat("status of order 1002", **kwargs)
        assert resp.status_code == 401 and resp.json() == {"detail": "Unauthorized"}, kwargs
    assert orch.llm.calls == []
    assert (await orch.client.post("/chat", json={"message": ""}, headers={"Authorization": f"Bearer {TOKEN_A}"})).status_code == 422
    assert (await orch.client.get("/healthz")).json() == {"ok": True}


async def test_unlisted_tool_rejected(make_orch):
    orch = await make_orch(FakeChatModel(gullible=True))  # obeys the injected note and asks for refund_order
    reply = await orch.reply("what is the note on order 1002?")
    last_batch = orch.llm.calls[-1]
    assert any('"unknown_tool"' in str(m.content) for m in last_batch)  # the tools node refused it
    assert "refund_order" in str(orch.llm.calls[-1][-2].tool_calls)  # the model really did ask for it
    assert await orch.order_status(1002) == "PAID"  # and nothing happened
    assert isinstance(reply, str)


async def test_tool_call_limit(make_orch):
    class Looping(FakeChatModel):  # a model stuck asking for the same tool forever
        def _choose_reply(self, messages):
            self.calls.append(list(messages))
            return AIMessage(content="", tool_calls=[_make_tool_call("get_order", {"order_id": 1002})])

    orch = await make_orch(Looping())
    assert await orch.reply("status of order 1002") == TOOL_LIMIT_REPLY
    assert len(orch.llm.calls) == 6  # six tool rounds; the limit then answers without calling the model again


async def test_extra_field_from_agent_is_rejected(make_orch):
    leaked = {"ok": True, "order": {"id": 1002, "status": "PAID", "total": 59.0, "placed_at": "2026-11-01T10:00:00Z",
                                    "has_note": False, "customer_email": "leak@example.com"}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"skills": [{"id": s} for s in READ_SKILLS]})
        reply = {"jsonrpc": "2.0", "id": 1, "result": {"message": {"parts": [{"data": leaked}]}}}
        return httpx.Response(200, json=reply)

    orch = await make_orch(agent_transport=httpx.MockTransport(handler))
    await orch.reply("status of order 1002")
    inputs = orch.model_inputs()
    assert not any("leak@example.com" in text for _, text in inputs)
    assert any("invalid_agent_response" in text for kind, text in inputs if kind == "tool")


async def test_agent_down_is_reported_as_data(make_orch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("agent is down")

    orch = await make_orch(agent_transport=httpx.MockTransport(handler))
    assert (await orch.chat("status of order 1002")).status_code == 200
    assert any("upstream_unavailable" in text for kind, text in orch.model_inputs() if kind == "tool")


async def test_card_discovery_retries():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise httpx.ConnectError("not up yet")
            return httpx.Response(200, json={"skills": [{"id": s} for s in READ_SKILLS]})
        return httpx.Response(200, json={"result": {"message": {"parts": [{"data": {"ok": True}}]}}})

    client = A2AClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), "http://agent", retry_delay=0)
    assert await client.call_skill("Bearer t", "merchant_a", "get_order", {"order_id": 1}) == {"ok": True}
    assert attempts["n"] == 2  # failed once, then succeeded
    await client.call_skill("Bearer t", "merchant_a", "get_order", {"order_id": 1})
    assert attempts["n"] == 2  # the card is fetched once, not per call

    missing = A2AClient(httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"skills": [{"id": "get_order"}]}))), "http://agent", retry_delay=0)
    with pytest.raises(AgentUnavailable):
        await missing.ensure_ready()


async def test_token_only_in_authorization_header():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen.append((request.headers["authorization"], request.content.decode()))
            return httpx.Response(200, json={"result": {"message": {"parts": [{"data": {"ok": True}}]}}})
        return httpx.Response(200, json={"skills": [{"id": s} for s in READ_SKILLS]})

    client = A2AClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), "http://agent", retry_delay=0)
    await client.call_skill("Bearer secret-token-xyz", "merchant_a", "get_order", {"order_id": 1})
    header, body = seen[0]
    assert header == "Bearer secret-token-xyz" and "secret-token-xyz" not in body
    assert json.loads(body)["params"]["message"]["parts"][0]["data"]["merchant_claim"] == "merchant_a"


def test_orchestrator_does_not_import_agent_code():
    forbidden = re.compile(r"^\s*(?:from|import)\s+(?:orders_agent|store|tools\.subagent_tools)\b", re.M)
    files = list(pathlib.Path("orchestrator").glob("*.py")) + list(pathlib.Path("tools/orchestrator_tools").glob("*.py"))
    assert files
    assert [str(f) for f in files if forbidden.search(f.read_text())] == []
