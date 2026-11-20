import os

import pytest

os.environ["LANGCHAIN_TRACING_V2"] = "false"


@pytest.fixture(autouse=True)
def _fake_provider(monkeypatch):
    """Tests must pass with no API key."""
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


# ---------- orders agent harness: real store app, agent app, raw JSON-RPC ----------
import uuid
from datetime import datetime, timezone

import httpx

from orders_agent.app import create_app as create_agent
from store.app import create_app as create_store

FIXED_NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
TOKEN_A, TOKEN_B, TOKEN_NOBODY = "tok_demo_merchant_a", "tok_demo_merchant_b", "tok_demo_nomerchant"


class AgentHarness:
    def __init__(self, client: httpx.AsyncClient, agent_app, store_client: httpx.AsyncClient):
        self.client, self.agent_app, self.store_client = client, agent_app, store_client

    def body(self, parts: list[dict]) -> dict:
        return {"jsonrpc": "2.0", "id": 1, "method": "SendMessage",
                "params": {"message": {"messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": parts}}}

    async def rpc(self, skill: str, args: dict | None = None, token: str | None = TOKEN_A, claim: str | None = None,
                  parts: list[dict] | None = None, header: str | None = None) -> httpx.Response:
        data = {"skill": skill, "args": args or {}}
        if claim is not None:
            data["merchant_claim"] = claim
        headers = {"A2A-Version": "1.0"}
        if header is not None:
            headers["Authorization"] = header
        elif token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return await self.client.post("/", json=self.body(parts if parts is not None else [{"data": data}]), headers=headers)

    async def call(self, skill: str, args: dict | None = None, token: str | None = TOKEN_A, claim: str | None = None) -> dict:
        """Send one skill call and return the data part of the reply."""
        resp = await self.rpc(skill, args, token, claim)
        assert resp.status_code == 200, resp.text
        return resp.json()["result"]["message"]["parts"][0]["data"]

    async def order_status(self, order_id: int, token: str = TOKEN_A) -> str:
        resp = await self.store_client.get("/orders/lookup", params={"id": order_id}, headers={"Authorization": f"Bearer {token}"})
        return resp.json()["status"]


@pytest.fixture
async def agent(tmp_path):
    store_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_store(str(tmp_path / "store.db"))), base_url="http://store")
    agent_app = create_agent(http_client=store_client, clock=lambda: FIXED_NOW)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=agent_app), base_url="http://agent")
    yield AgentHarness(client, agent_app, store_client)
    await client.aclose()
    await store_client.aclose()


# ---------- orchestrator harness: real store + real orders agent + orchestrator, all in-process ----------
from contextlib import AsyncExitStack

from orchestrator.app import create_app as create_orchestrator
from orchestrator.fake_llm import FakeChatModel


class OrchHarness:
    def __init__(self, client, llm, store_http, agent_app, a2a_http, db_path=None, stack=None):
        self.client, self.llm, self.store_http, self.agent_app, self.a2a_http = client, llm, store_http, agent_app, a2a_http
        self.db_path, self.stack = db_path, stack

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
        return await self.client.post("/chat", json=body, headers=headers)

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


@pytest.fixture
async def make_orch(tmp_path):
    """Factory: build the whole system around any LLM, or around a custom agent transport."""
    stacks = []

    async def build(llm=None, agent_transport=None):
        llm = llm or FakeChatModel()
        store_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_store(str(tmp_path / "store.db"))), base_url="http://store")
        agent_app = create_agent(http_client=store_http, clock=lambda: FIXED_NOW)
        a2a_http = httpx.AsyncClient(transport=agent_transport or httpx.ASGITransport(app=agent_app))
        db_path = str(tmp_path / "orchestrator.db")  # same path on every build, so a second build is a "restart"
        orch_app = create_orchestrator(llm=llm, store_http=store_http, a2a_http=a2a_http, a2a_retry_delay=0, db_path=db_path)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=orch_app), base_url="http://orch")
        stack = AsyncExitStack()  # ASGITransport does not run the lifespan, so enter it here
        await stack.enter_async_context(orch_app.router.lifespan_context(orch_app))
        for closer in (client, a2a_http, store_http):
            stack.push_async_callback(closer.aclose)
        stacks.append(stack)
        return OrchHarness(client, llm, store_http, agent_app, a2a_http, db_path, stack)

    yield build
    for stack in stacks:
        await stack.aclose()  # does nothing if the test already closed it


@pytest.fixture
async def orch(make_orch):
    return await make_orch()
