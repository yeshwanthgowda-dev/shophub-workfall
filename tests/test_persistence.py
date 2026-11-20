import glob
import sqlite3

from langchain_core.messages import HumanMessage

from tests.conftest import TOKEN_A, TOKEN_B

TOKEN_STRINGS = [b"tok_demo_merchant_a", b"tok_demo_merchant_b", b"tok_demo_nomerchant", b"Bearer"]


def files_containing(db_path: str, needles: list[bytes]) -> list[str]:
    """Names of the .db, -wal and -shm files that contain any needle."""
    return [f.rsplit("/", 1)[-1] for f in glob.glob(db_path + "*") if any(n in open(f, "rb").read() for n in needles)]


async def test_token_not_in_sqlite(orch):
    thread_id = (await orch.chat("status of order 1002")).json()["thread_id"]
    await orch.reply("what is the note on order 1002?", thread_id=thread_id)
    await orch.reply("list all orders", token=TOKEN_B)
    assert glob.glob(orch.db_path + "-wal")  # the WAL file exists, so the scan below really covers it
    assert files_containing(orch.db_path, TOKEN_STRINGS) == []  # app still running: data is in the -wal file
    await orch.close()
    assert files_containing(orch.db_path, TOKEN_STRINGS) == []  # after shutdown: data is in the main file


async def test_token_scan_detects_a_planted_token(orch):
    """Positive control: the scan must find a token that really is in the database, in the -wal file."""
    await orch.reply("status of order 1002")
    planted = sqlite3.connect(orch.db_path)
    planted.execute("INSERT INTO thread_bindings (thread_id, merchant) VALUES ('planted', 'tok_demo_merchant_a')")
    planted.commit()
    hits = files_containing(orch.db_path, TOKEN_STRINGS)
    planted.close()
    assert hits, "the scan did not see a planted token"


async def test_token_in_configurable_would_leak(orch):
    """Why the token travels in a ContextVar: LangGraph copies `configurable` into the checkpoint."""
    graph = orch.client._transport.app.state.graph
    await graph.ainvoke({"messages": [HumanMessage("hi")]}, {"configurable": {"thread_id": "leak", "auth": "Bearer tok_demo_merchant_a"}})
    assert files_containing(orch.db_path, TOKEN_STRINGS)


async def test_typed_pii_not_in_sqlite(orch):
    resp = await orch.chat("refund the order of maria@example.com or call 415-555-0134")
    assert resp.status_code == 200 and resp.json()["thread_id"] is None  # refused before any thread or checkpoint exists
    await orch.close()
    assert files_containing(orch.db_path, [b"maria@example.com", b"415-555-0134"]) == []


async def test_restart_resume(make_orch):
    first = await make_orch()
    thread_id = (await first.chat("status of order 1002")).json()["thread_id"]
    await first.close()

    second = await make_orch()  # new app, graph and saver, same file
    assert (await second.chat("and its total?", thread_id=thread_id)).status_code == 200
    seen = [text for kind, text in second.model_inputs() if kind == "human"]
    assert "status of order 1002" in seen  # the first turn came back from SQLite
    history = (await second.client.get(f"/threads/{thread_id}", headers={"Authorization": f"Bearer {TOKEN_A}"})).json()
    users = [m["text"] for m in history["messages"] if m["role"] == "user"]
    assert users == ["status of order 1002", "and its total?"]


async def test_foreign_thread_404(orch):
    thread_id = (await orch.chat("status of order 1002")).json()["thread_id"]
    unknown = "00000000-0000-4000-8000-000000000000"
    as_b = {"Authorization": f"Bearer {TOKEN_B}"}
    foreign_get = await orch.client.get(f"/threads/{thread_id}", headers=as_b)
    unknown_get = await orch.client.get(f"/threads/{unknown}", headers=as_b)
    foreign_chat = await orch.chat("hello", token=TOKEN_B, thread_id=thread_id)
    unknown_chat = await orch.chat("hello", token=TOKEN_B, thread_id=unknown)
    for resp in (foreign_get, unknown_get, foreign_chat, unknown_chat):
        assert resp.status_code == 404 and resp.json() == {"detail": "Thread not found"}
    model_calls = len(orch.llm.calls)
    await orch.chat("hello", token=TOKEN_B, thread_id=thread_id)
    assert len(orch.llm.calls) == model_calls  # the model never ran for the foreign caller


async def test_client_cannot_choose_a_new_thread_id(orch):
    resp = await orch.chat("status of order 1002", thread_id="my-own-id")
    assert resp.status_code == 404


async def test_history_is_redacted(orch):
    thread_id = (await orch.chat("what is the note on order 1002?")).json()["thread_id"]
    history = (await orch.client.get(f"/threads/{thread_id}", headers={"Authorization": f"Bearer {TOKEN_A}"})).json()
    roles = [m["role"] for m in history["messages"]]
    assert "tool" in roles
    assert all(set(m) <= {"role", "text", "tool_calls", "name", "ok"} for m in history["messages"])
    tool_entries = [m for m in history["messages"] if m["role"] == "tool"]
    assert tool_entries == [{"role": "tool", "name": "get_order_note", "ok": True}]  # no tool result content is returned


async def test_thread_endpoint_needs_valid_token(orch):
    thread_id = (await orch.chat("hi")).json()["thread_id"]
    assert (await orch.client.get(f"/threads/{thread_id}")).status_code == 401
    assert (await orch.client.get(f"/threads/{thread_id}", headers={"Authorization": "Bearer nope"})).status_code == 401
