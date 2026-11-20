"""Chat API (module 3: plain JSON, one turn per call; streaming and memory come in modules 6 and 4).

Run:  uvicorn --factory orchestrator.app:create_app --port 8000
"""
import logging
import os
import uuid

import httpx
from fastapi import FastAPI, Header, HTTPException
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.errors import GraphRecursionError
from pydantic import BaseModel, Field

from orchestrator.a2a_client import A2AClient, AgentUnavailable
from orchestrator.auth import StoreAuth, StoreUnavailable
from orchestrator.context import current_auth, current_merchant
from orchestrator.graph import build_graph
from orchestrator.llm import make_llm
from tools.orchestrator_tools.read_tools import make_read_tools

log = logging.getLogger("orchestrator")
RECURSION_LIMIT = 16
GAVE_UP_REPLY = "I couldn't finish that request. Please try a simpler question."


class ChatRequest(BaseModel):
    thread_id: str | None = None
    message: str = Field(min_length=1, max_length=4000)


def create_app(llm=None, store_http: httpx.AsyncClient | None = None, a2a_http: httpx.AsyncClient | None = None,
               a2a_retry_delay: float = 0.5) -> FastAPI:
    """`llm`, `store_http` and `a2a_http` are injectable so tests can use the fake model and in-process services."""
    llm = llm or make_llm(max_tokens=1024)
    store_http = store_http or httpx.AsyncClient(base_url=os.environ.get("STORE_URL", "http://localhost:8001"), timeout=5.0)
    a2a_http = a2a_http or httpx.AsyncClient(timeout=10.0)
    store_auth = StoreAuth(store_http)
    a2a = A2AClient(a2a_http, os.environ.get("A2A_PUBLIC_URL", "http://localhost:8002"), retry_delay=a2a_retry_delay)

    async def call_skill(skill: str, args: dict) -> dict:
        """Used by the tools. Reads the caller from the request context, so no token passes through tool arguments."""
        try:
            return await a2a.call_skill(current_auth.get(), current_merchant.get(), skill, args)
        except AgentUnavailable:
            log.warning("orders agent unavailable for skill %s", skill)
            return {"ok": False, "code": "upstream_unavailable"}

    graph = build_graph(llm, make_read_tools(call_skill))
    app = FastAPI(title="ShopHub orchestrator")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.post("/chat")
    async def chat(body: ChatRequest, authorization: str | None = Header(default=None)):
        if not authorization:
            raise HTTPException(status_code=401, detail="Unauthorized")
        try:
            me = await store_auth.whoami(authorization)
        except StoreUnavailable:
            raise HTTPException(status_code=503, detail="Service unavailable")
        if me is None:  # invalid token, or a token with no merchant
            raise HTTPException(status_code=401, detail="Unauthorized")

        auth_token, merchant_token = current_auth.set(authorization), current_merchant.set(me["merchant"])
        try:
            result = await graph.ainvoke({"messages": [HumanMessage(body.message)]}, {"recursion_limit": RECURSION_LIMIT})
            last = result["messages"][-1]
            reply = last.content if isinstance(last, AIMessage) else GAVE_UP_REPLY
        except GraphRecursionError:
            reply = GAVE_UP_REPLY
        finally:
            current_auth.reset(auth_token)
            current_merchant.reset(merchant_token)
        return {"thread_id": body.thread_id or str(uuid.uuid4()), "reply": reply}

    return app
