import asyncio

import httpx
from orchestrator.approval_nodes import NOTE_WARNING, REFUSAL
from evals.system import Scripted
from orchestrator.fake_llm import FakeChatModel
from orchestrator.verifier import fake_verify
from tests.conftest import TOKEN_A, TOKEN_B
from tests.test_persistence import TOKEN_STRINGS, files_containing

NOT_FOUND = {"detail": "Proposal not found"}


async def start(orch, message="refund order 1002", token=TOKEN_A, thread_id=None):
    """Make a proposal. Returns (thread_id, proposal dict)."""
    body = await orch.propose(message, token, thread_id)
    assert body["reply"] is None and body["proposal"], body
    return body["thread_id"], body["proposal"]


async def test_proposal_approve_reject(orch):
    thread, proposal = await start(orch, "refund order 1002")
    assert proposal["action"] == "refund" and proposal["amount"] == 59.0
    assert (proposal["from_status"], proposal["to_status"]) == ("PAID", "REFUNDED")
    assert await orch.order_status(1002) == "PAID"  # a proposal changes nothing
    resp = await orch.decide("approve", thread, proposal["proposal_id"])
    assert resp.status_code == 200 and resp.json()["status"] == "executed" and resp.json()["outcome"] == "OK"
    assert await orch.order_status(1002) == "REFUNDED"

    thread, proposal = await start(orch, "cancel order 1008")
    resp = await orch.decide("reject", thread, proposal["proposal_id"])
    assert resp.status_code == 200 and resp.json()["status"] == "rejected"
    assert await orch.order_status(1008) == "PAID"  # rejecting does nothing to the order


async def test_expiry(orch):
    thread, proposal = await start(orch)
    orch.clock.advance(901)
    resp = await orch.decide("approve", thread, proposal["proposal_id"])
    assert resp.status_code == 409 and resp.json() == {"code": "expired"}
    assert await orch.order_status(1002) == "PAID"


async def test_single_use(orch):
    thread, proposal = await start(orch)
    pid = proposal["proposal_id"]
    assert (await orch.decide("approve", thread, pid)).status_code == 200
    for action in ("approve", "reject"):
        resp = await orch.decide(action, thread, pid)
        assert resp.status_code == 409 and resp.json() == {"code": "already_used"}
    thread2, second = await start(orch, "refund order 1003")
    await orch.decide("reject", thread2, second["proposal_id"])
    resp = await orch.decide("approve", thread2, second["proposal_id"])
    assert resp.status_code == 409 and resp.json() == {"code": "rejected"}
    assert await orch.order_status(1003) == "PAID"


async def test_double_approve(orch):
    thread, proposal = await start(orch)
    first, second = await asyncio.gather(*(orch.decide("approve", thread, proposal["proposal_id"]) for _ in range(2)))
    assert sorted([first.status_code, second.status_code]) == [200, 409]  # exactly one wins the claim
    assert (await orch.proposals.get(proposal["proposal_id"]))["status"] == "executed"


async def test_wrong_merchant_approver(orch):
    thread, proposal = await start(orch)
    pid = proposal["proposal_id"]
    foreign = await orch.decide("approve", thread, pid, TOKEN_B)
    unknown = await orch.decide("approve", thread, "no-such-proposal", TOKEN_B)
    assert foreign.status_code == unknown.status_code == 404 and foreign.json() == unknown.json()
    assert (await orch.proposals.get(pid))["status"] == "pending" and await orch.order_status(1002) == "PAID"
    assert (await orch.decide("approve", thread, pid, None)).status_code == 401
    assert (await orch.decide("approve", thread, pid, "nope")).status_code == 401


async def test_wrong_thread(orch):
    thread_one, proposal = await start(orch)
    thread_two = (await orch.chat("hello")).json()["thread_id"]
    resp = await orch.decide("approve", thread_two, proposal["proposal_id"])
    assert resp.status_code == 404 and resp.json() == NOT_FOUND
    assert (await orch.proposals.get(proposal["proposal_id"]))["status"] == "pending"


async def test_same_merchant_other_user_may_approve(orch):
    """The merchant decides who is trusted: any valid token of the same merchant can approve."""
    thread, proposal = await start(orch)
    assert (await orch.decide("approve", thread, proposal["proposal_id"], TOKEN_A)).status_code == 200


async def test_duplicate_pending_proposal_blocked(orch):
    await start(orch, "refund order 1002")
    reply = await orch.reply("refund order 1002")  # a second thread, same order
    assert "proposal_already_pending" in reply
    async with orch.proposals.conn.execute("SELECT COUNT(*) FROM proposals WHERE status = 'pending'") as cur:
        assert (await cur.fetchone())[0] == 1


async def test_409_as_data(orch):
    thread, proposal = await start(orch, "refund order 1006")  # already CANCELLED: the store refuses the refund
    resp = await orch.decide("approve", thread, proposal["proposal_id"])
    assert resp.status_code == 200
    assert resp.json()["status"] == "failed" and resp.json()["outcome"] == "INVALID_STATE" and resp.json()["current_status"] == "CANCELLED"


async def test_order_changed_after_proposal_is_not_executed(orch):
    thread, proposal = await start(orch, "refund order 1002")
    await orch.store_http.post("/orders/cancel", json={"id": 1002}, headers={"Authorization": f"Bearer {TOKEN_A}"})
    resp = await orch.decide("approve", thread, proposal["proposal_id"])
    assert resp.json()["outcome"] == "INVALID_STATE" and resp.json()["current_status"] == "CANCELLED"
    assert await orch.order_status(1002) == "CANCELLED"  # not refunded


async def test_execute_failure_marks_failed(orch):
    thread, proposal = await start(orch)

    def down(request):
        raise httpx.ConnectError("agent is down")

    orch.a2a_http._transport = httpx.MockTransport(down)
    resp = await orch.decide("approve", thread, proposal["proposal_id"])
    assert resp.status_code == 200 and resp.json()["status"] == "failed" and resp.json()["outcome"] == "UPSTREAM_UNAVAILABLE"
    assert await orch.order_status(1002) == "PAID"


async def test_expired_pending_does_not_block_new_proposal(orch):
    _, old = await start(orch)
    orch.clock.advance(901)
    thread, new = await start(orch, "refund order 1002")  # another thread, same order
    assert new["proposal_id"] != old["proposal_id"]
    assert (await orch.proposals.get(old["proposal_id"]))["status"] == "expired"
    assert (await orch.decide("approve", thread, new["proposal_id"])).status_code == 200


async def test_new_message_supersedes_pending_proposal(orch):
    thread, proposal = await start(orch)
    assert (await orch.chat("what is the status of order 1003?", thread_id=thread)).status_code == 200
    row = await orch.proposals.get(proposal["proposal_id"])
    assert (row["status"], row["outcome"]) == ("rejected", "SUPERSEDED")
    assert (await orch.decide("approve", thread, proposal["proposal_id"])).status_code == 409
    assert await orch.order_status(1002) == "PAID"


async def test_pending_proposal_survives_restart(make_orch):
    first = await make_orch()
    thread, proposal = await start(first)
    await first.close()
    second = await make_orch()  # new app, graph and saver on the same file
    assert (await second.decide("approve", thread, proposal["proposal_id"])).json()["status"] == "executed"
    assert await second.order_status(1002) == "REFUNDED"


async def test_token_not_in_sqlite_after_interrupt_and_resume(orch):
    thread, proposal = await start(orch)  # the run is paused at interrupt()
    assert files_containing(orch.db_path, TOKEN_STRINGS) == []
    await orch.decide("approve", thread, proposal["proposal_id"])  # resumed and executed with the approver's token
    assert files_containing(orch.db_path, TOKEN_STRINGS) == []
    await orch.close()
    assert files_containing(orch.db_path, TOKEN_STRINGS) == []


# ---------- the model cannot execute ----------

async def test_model_cannot_execute_refund(make_orch):
    orch = await make_orch(FakeChatModel(gullible=True))  # a note tells it to refund, and it obeys with refund_order
    await orch.reply("what is the note on order 1002?")
    assert any('"unknown_tool"' in text for kind, text in orch.model_inputs() if kind == "tool")
    assert await orch.order_status(1002) == "PAID"
    async with orch.proposals.conn.execute("SELECT COUNT(*) FROM proposals") as cur:
        assert (await cur.fetchone())[0] == 0


async def test_injection_not_followed(make_orch):
    orch = await make_orch(FakeChatModel(gullible=True, gullible_tool="propose_refund"))
    reply = await orch.reply("what is the note on order 1002?")  # the user did not ask for a refund
    assert reply == REFUSAL + NOTE_WARNING
    assert await orch.order_status(1002) == "PAID"
    async with orch.proposals.conn.execute("SELECT COUNT(*) FROM proposals") as cur:
        assert (await cur.fetchone())[0] == 0


async def test_mismatched_action_refused(make_orch):
    model = Scripted(script=[[("propose_refund", {"order_id": 1003})]])  # the user named 1002
    orch = await make_orch(model)
    assert await orch.reply("refund order 1002") == REFUSAL
    assert await orch.order_status(1003) == "PAID"


# ---------- Gate A wiring ----------

class Recorder:
    """Wraps the fake verifier and remembers what it was given."""
    def __init__(self, answer=None, error=None):
        self.seen, self.answer, self.error = [], answer, error

    async def __call__(self, human_messages, action):
        self.seen.append((list(human_messages), dict(action)))
        if self.error:
            raise self.error
        return fake_verify(human_messages, action) if self.answer is None else self.answer


async def test_verifier_input_has_no_tool_output(make_orch):
    recorder = Recorder()
    model = Scripted(script=[[("get_order_note", {"order_id": 1002})], [("propose_refund", {"order_id": 1002})]])
    orch = await make_orch(model, verifier=recorder)
    await orch.propose("read the note on order 1002 then refund order 1002")
    marker = "UNTRUSTED_CUSTOMER_NOTE"
    assert any(marker in text for kind, text in orch.model_inputs() if kind == "tool")  # positive control: the marker was in context
    humans, _ = recorder.seen[0]
    assert marker not in str(recorder.seen) and "ignore your instructions" not in str(recorder.seen)


async def test_verifier_excludes_assistant_messages(make_orch):
    recorder = Recorder()
    orch = await make_orch(verifier=recorder)
    thread = (await orch.chat("status of order 1002")).json()["thread_id"]
    await orch.propose("refund it", thread_id=thread)
    humans, action = recorder.seen[0]
    assert humans == ["status of order 1002", "refund it"] and action == {"action": "refund", "order_id": 1002}


async def test_verifier_fail_closed(make_orch):
    for verifier in (Recorder(error=asyncio.TimeoutError()), Recorder(error=ValueError("malformed")), Recorder(answer=False)):
        orch = await make_orch(verifier=verifier)
        assert await orch.reply("refund order 1002") == REFUSAL
        assert await orch.order_status(1002) == "PAID"


async def test_refusal_streams_template_and_ends_run(make_orch):
    orch = await make_orch(verifier=Recorder(answer=False))
    resp = await orch.chat("refund order 1002")
    assert resp.json()["reply"] == REFUSAL and "proposal" not in resp.json()
    assert len(orch.llm.calls) == 1  # the model got no further turn after the refusal


async def test_note_warning_variant_when_notes_read(make_orch):
    model = Scripted(script=[[("get_order_note", {"order_id": 1002})], [("propose_refund", {"order_id": 1002})]])
    orch = await make_orch(model, verifier=Recorder(answer=False))
    assert await orch.reply("what is the note on 1002, and refund it") == REFUSAL + NOTE_WARNING
    plain = await make_orch(verifier=Recorder(answer=False))
    assert await plain.reply("refund order 1002") == REFUSAL  # no note read: no warning


async def test_tainted_turn_with_matching_request_proposes(make_orch):
    model = Scripted(script=[[("get_order_note", {"order_id": 1002})], [("propose_refund", {"order_id": 1002})]])
    orch = await make_orch(model)
    _, proposal = await start(orch, "read the note on order 1002 and refund order 1002")
    assert proposal["note_flagged"] is True  # allowed, but flagged for the human


async def test_second_propose_same_turn_refused(make_orch):
    model = Scripted(script=[[("propose_refund", {"order_id": 1002}), ("propose_cancel", {"order_id": 1003})]])
    orch = await make_orch(model)
    thread, proposal = await start(orch, "refund order 1002 and cancel order 1003")
    history = (await orch.client.get(f"/threads/{thread}", headers={"Authorization": f"Bearer {TOKEN_A}"})).json()
    assert [m["ok"] for m in history["messages"] if m["role"] == "tool"] == [True, False]
    async with orch.proposals.conn.execute("SELECT COUNT(*) FROM proposals") as cur:
        assert (await cur.fetchone())[0] == 1


async def test_pronoun_carry_over_through_the_graph(orch):
    thread = (await orch.chat("status of order 1002")).json()["thread_id"]
    _, proposal = await start(orch, "refund it", thread_id=thread)
    assert proposal["order_id"] == 1002
