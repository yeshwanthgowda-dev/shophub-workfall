import asyncio

import httpx
import pytest
from langgraph.errors import GraphRecursionError

import orchestrator.app as app_module
from orchestrator.fake_llm import FakeChatModel
from orchestrator.streaming import GAVE_UP_REPLY, graph_events
from shared.contracts import READ_SKILLS
from store.seed import ORDERS
from tests.conftest import TOKEN_A, TOKEN_B
from tests.test_orchestrator import A_ORDERS
from tests.test_proposals import REFUSAL, Recorder


def names(result) -> list[str]:
    return [name for name, _ in result.events]


async def test_sse_events_order(orch):
    result = await orch.chat("what is the status of order 1002?")
    seq = names(result)
    assert seq[0] == "tool_started" and seq[1] == "tool_finished" and seq[-1] == "done"
    assert seq.count("done") == 1 and seq.count("token") > 1  # the answer arrives in pieces
    assert set(seq[2:-1]) == {"token"}  # after the tool call: only tokens, then done
    started, finished = result.events[0][1], result.events[1][1]
    assert started == {"name": "get_order", "call_id": finished["call_id"]} and finished["ok"] is True
    done = result.events[-1][1]
    assert done["thread_id"] and done["usage"]["total_tokens"] > 0


async def test_sse_proposal_ends_the_stream(orch):
    result = await orch.chat("refund order 1002")
    assert names(result) == ["tool_started", "tool_finished", "proposal", "done"]  # no token after the pause
    proposal = result.events[2][1]
    assert proposal["action"] == "refund" and proposal["order_id"] == 1002 and proposal["note_flagged"] is False


async def test_sse_http_headers(orch):
    resp = await orch.client.post("/chat", json={"message": "status of order 1002"}, headers={"Authorization": f"Bearer {TOKEN_A}"})
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-cache" and resp.headers["x-accel-buffering"] == "no"


async def test_tool_finished_carries_no_result(orch):
    result = await orch.chat("what is the note on order 1002?")
    finished = [data for name, data in result.events if name == "tool_finished"]
    assert finished and all(set(data) == {"name", "call_id", "ok"} for data in finished)
    assert "UNTRUSTED_CUSTOMER_NOTE" not in str([data for name, data in result.events if name != "token"])


async def test_stream_needs_a_valid_token_before_it_starts(orch):
    for kwargs in ({"token": None}, {"token": "nope"}):
        resp = await orch.chat("status of order 1002", **kwargs)
        assert resp.status_code == 401 and resp.headers["content-type"].startswith("application/json")
    assert orch.llm.calls == []


async def test_sse_no_pii(make_orch):
    secrets = [v for o in ORDERS for v in (o["customer_email"], o["customer_phone"], o["customer_name"])]
    secrets += ["tok_demo", "Bearer"]
    for token, orders in ((TOKEN_A, A_ORDERS), (TOKEN_B, [o for o in ORDERS if o["merchant"] == "merchant_b"])):
        orch = await make_orch()
        questions = ["list all orders", "what did Maria order?", "refund order 1002 please"]
        questions += [f"what is the note on order {o['id']}?" for o in orders if o["customer_note"].strip()]
        for question in questions:
            text = (await orch.chat(question, token)).text
            for secret in secrets:
                assert secret not in text, (secret, question)
    typed = (await orch.chat("refund the order of maria@example.com")).text
    assert "maria@example.com" not in typed  # a typed email is refused, not echoed


async def test_sse_ends_with_done_or_error(make_orch):
    for message, kwargs in (("status of order 1002", {}), ("status of order 9999", {}), ("refund order 1002", {}),
                            ("call me on 415-555-0134", {}), ("hello there", {})):
        result = await (await make_orch(**kwargs)).chat(message)
        assert names(result)[-1] == "done", message
    refused = await (await make_orch(verifier=Recorder(answer=False))).chat("refund order 1002")
    assert names(refused)[-1] == "done" and ("token", {"text": REFUSAL}) in refused.events


async def test_agent_down_still_ends_with_done(make_orch):
    def down(request):
        raise httpx.ConnectError("agent is down")

    orch = await make_orch(agent_transport=httpx.MockTransport(down))
    result = await orch.chat("status of order 1002")
    assert [data["ok"] for name, data in result.events if name == "tool_finished"] == [False]
    assert names(result)[-1] == "done"


class BrokenGraph:
    def __init__(self, error):
        self.error = error

    async def astream(self, *args, **kwargs):
        raise self.error
        yield  # makes this an async generator


async def test_unexpected_error_ends_with_error_event():
    events = [e async for e in graph_events(BrokenGraph(ValueError("secret internal detail")), {}, {}, None, "t")]
    assert events == [("error", {"code": "internal_error"})]  # generic code, no details, no done


async def test_recursion_limit_streams_gave_up_then_done():
    events = [e async for e in graph_events(BrokenGraph(GraphRecursionError("x")), {}, {}, None, "t")]
    assert [name for name, _ in events] == ["token", "done"] and events[0][1] == {"text": GAVE_UP_REPLY}


async def test_heartbeat_while_a_tool_is_slow(make_orch, monkeypatch):
    monkeypatch.setattr(app_module, "HEARTBEAT_SECONDS", 0.05)

    async def slow_agent(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"skills": [{"id": s} for s in READ_SKILLS]})
        await asyncio.sleep(0.3)
        return httpx.Response(200, json={"result": {"message": {"parts": [{"data": {"ok": False, "code": "not_found"}}]}}})

    orch = await make_orch(agent_transport=httpx.MockTransport(slow_agent))
    result = await orch.chat("status of order 1002")
    assert ": ping" in result.text and names(result)[-1] == "done"  # the comment lines are not events
