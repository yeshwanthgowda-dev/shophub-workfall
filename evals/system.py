"""Builds the whole system in-process (real store, real orders agent, orchestrator) for pytest and the eval runner.

No network and no API key: the services talk through httpx.ASGITransport and the LLM is the fake one by default.
"""
import json
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone

import httpx
from langchain_core.messages import AIMessage

from orchestrator.app import create_app as create_orchestrator
from orchestrator.fake_llm import FakeChatModel, _make_tool_call
from orders_agent.app import create_app as create_agent
from store.app import create_app as create_store

FIXED_NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
TOKEN_A, TOKEN_B, TOKEN_NOBODY = "tok_demo_merchant_a", "tok_demo_merchant_b", "tok_demo_nomerchant"


class Scripted(FakeChatModel):
    """A model that follows a script of tool calls, one AIMessage per agent step, then goes back to the fake rules."""
    script: list = []

    def _choose_reply(self, messages):
        if self.script and len(self.calls) < len(self.script):
            step = self.script[len(self.calls)]
            self.calls.append(list(messages))
            return AIMessage(content="", tool_calls=[_make_tool_call(name, args) for name, args in step])
        return super()._choose_reply(messages)


def parse_sse(text: str) -> list[tuple[str, dict]]:
    """[(event name, data)] from a server-sent-events body. Comment lines (heartbeats) are skipped."""
    events = []
    for block in text.split("\n\n"):
        lines = [line for line in block.splitlines() if line and not line.startswith(":")]
        if lines:
            events.append((lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))))
    return events


class ChatResult:
    """A /chat stream read to the end, shaped like the old JSON reply so tests can keep using .json()."""
    status_code = 200

    def __init__(self, text: str):
        self.text, self.events = text, parse_sse(text)

    def json(self) -> dict:
        proposals = [data for name, data in self.events if name == "proposal"]
        done = next((data for name, data in self.events if name == "done"), {})
        reply = None if proposals else "".join(data["text"] for name, data in self.events if name == "token")
        return {"thread_id": done.get("thread_id"), "reply": reply, **({"proposal": proposals[0]} if proposals else {})}


class Clock:
    """The orchestrator's clock in tests: starts at FIXED_NOW and only moves when a test advances it."""
    def __init__(self):
        self.now = FIXED_NOW

    def __call__(self):
        return self.now

    def advance(self, seconds: float):
        self.now += timedelta(seconds=seconds)


class System:
    def __init__(self, client, llm, store_http, agent_app, a2a_http, db_path=None, stack=None, clock=None):
        self.client, self.llm, self.store_http, self.agent_app, self.a2a_http = client, llm, store_http, agent_app, a2a_http
        self.db_path, self.stack, self.clock = db_path, stack, clock

    @property
    def proposals(self):
        return self.client._transport.app.state.proposals

    async def decide(self, action: str, thread_id: str, proposal_id: str, token: str | None = TOKEN_A) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return await self.client.post(f"/threads/{thread_id}/proposals/{proposal_id}/{action}", headers=headers)

    async def propose(self, message: str, token: str = TOKEN_A, thread_id: str | None = None) -> dict:
        """Send a message that should lead to a proposal. Returns the /chat JSON, which carries `proposal`."""
        resp = await self.chat(message, token, thread_id=thread_id)
        assert resp.status_code == 200, resp.text
        return resp.json()

    async def close(self):
        """Shut the orchestrator down (runs its lifespan exit, closing the SQLite connections)."""
        await self.stack.aclose()

    async def chat(self, message: str, token: str | None = TOKEN_A, header: str | None = None,
                   thread_id: str | None = None) -> httpx.Response:
        headers = {}
        if header is not None:
            headers["Authorization"] = header
        elif token is not None:
            headers["Authorization"] = f"Bearer {token}"
        body = {"message": message, **({"thread_id": thread_id} if thread_id else {})}
        resp = await self.client.post("/chat", json=body, headers=headers)
        # A stream becomes a ChatResult; an error answered before the stream (401, 404, 422) stays a plain response.
        return ChatResult(resp.text) if resp.headers.get("content-type", "").startswith("text/event-stream") else resp

    async def reply(self, message: str, token: str | None = TOKEN_A, thread_id: str | None = None) -> str:
        resp = await self.chat(message, token, thread_id=thread_id)
        assert resp.status_code == 200, resp.text
        return resp.json()["reply"]

    def model_inputs(self) -> list[tuple[str, str]]:
        """Everything the model was ever given: (message type, text including tool-call arguments)."""
        seen = []
        for batch in self.llm.calls:
            for message in batch:
                seen.append((message.type, str(message.content) + str(getattr(message, "tool_calls", ""))))
        return seen

    async def order_status(self, order_id: int, token: str = TOKEN_A) -> str:
        resp = await self.store_http.get("/orders/lookup", params={"id": order_id}, headers={"Authorization": f"Bearer {token}"})
        return resp.json()["status"]




async def build_system(directory, llm=None, agent_transport=None, verifier=None) -> System:
    """A fresh system whose files live in `directory`. Building twice on the same directory is a restart."""
    llm = llm or FakeChatModel()
    store_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_store(str(directory / "store.db"))), base_url="http://store")
    agent_app = create_agent(http_client=store_http, clock=lambda: FIXED_NOW)
    a2a_http = httpx.AsyncClient(transport=agent_transport or httpx.ASGITransport(app=agent_app))
    clock = Clock()
    db_path = str(directory / "orchestrator.db")
    orch_app = create_orchestrator(llm=llm, store_http=store_http, a2a_http=a2a_http, a2a_retry_delay=0, db_path=db_path,
                                   verifier=verifier, clock=clock, proposal_ttl_seconds=900)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=orch_app), base_url="http://orch")
    stack = AsyncExitStack()  # ASGITransport does not run the lifespan, so enter it here
    await stack.enter_async_context(orch_app.router.lifespan_context(orch_app))
    for closer in (client, a2a_http, store_http):
        stack.push_async_callback(closer.aclose)
    return System(client, llm, store_http, agent_app, a2a_http, db_path, stack, clock)
