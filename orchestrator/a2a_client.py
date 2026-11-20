"""Calls the orders agent over A2A (JSON-RPC `SendMessage`). Plain httpx, about 25 lines of protocol.

The token goes only in the Authorization header, set per call. The endpoint is the URL we are
configured with, never a URL read from the agent card, so a bad card cannot redirect the token.
"""
import asyncio
import uuid

import httpx

from shared.contracts import READ_SKILLS


class AgentUnavailable(Exception):
    """The orders agent could not be reached, refused the call, or sent a protocol error."""


class A2AClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str, retries: int = 3, retry_delay: float = 0.5):
        self.http, self.base_url = http, base_url.rstrip("/")
        self.retries, self.retry_delay = retries, retry_delay
        self.ready = False

    async def ensure_ready(self) -> None:
        """Fetch the agent card once, with retries (the agent may still be starting), and check the skills."""
        if self.ready:
            return
        last_error = "no attempt"
        for attempt in range(self.retries):
            try:
                resp = await self.http.get(f"{self.base_url}/.well-known/agent-card.json")
                if resp.status_code == 200:
                    offered = {skill["id"] for skill in resp.json().get("skills", [])}
                    missing = set(READ_SKILLS) - offered
                    if missing:
                        raise AgentUnavailable(f"agent card lacks skills: {sorted(missing)}")
                    self.ready = True
                    return
                last_error = f"card answered {resp.status_code}"
            except httpx.TransportError as exc:
                last_error = str(exc)
            if attempt + 1 < self.retries:
                await asyncio.sleep(self.retry_delay)
        raise AgentUnavailable(last_error)

    async def call_skill(self, auth: str, merchant: str, skill: str, args: dict) -> dict:
        await self.ensure_ready()
        body = {
            "jsonrpc": "2.0", "id": 1, "method": "SendMessage",
            "params": {"message": {
                "messageId": str(uuid.uuid4()), "role": "ROLE_USER",
                "parts": [{"data": {"skill": skill, "args": args, "merchant_claim": merchant}}],
            }},
        }
        try:
            resp = await self.http.post(f"{self.base_url}/", json=body, headers={"Authorization": auth, "A2A-Version": "1.0"})
        except httpx.TransportError as exc:
            raise AgentUnavailable(str(exc)) from exc
        if resp.status_code != 200:
            raise AgentUnavailable(f"agent answered {resp.status_code}")
        reply = resp.json()
        if "error" in reply:  # protocol fault, not a business outcome
            raise AgentUnavailable("agent returned a protocol error")
        try:
            return reply["result"]["message"]["parts"][0]["data"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AgentUnavailable("unexpected reply shape") from exc
