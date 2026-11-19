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
