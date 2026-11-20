"""Chat API (plain JSON, one turn per call; streaming comes in module 6). Conversations persist in SQLite (module 4).

Run:  uvicorn --factory orchestrator.app:create_app --port 8000
"""
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphRecursionError
from pydantic import BaseModel, Field

from orchestrator.a2a_client import A2AClient, AgentUnavailable
from orchestrator.auth import StoreAuth, StoreUnavailable
from orchestrator.context import current_auth, current_merchant
from orchestrator.db import ThreadBindings, connect
from orchestrator.graph import PII_REFUSAL, build_graph
from orchestrator.llm import make_llm
from sanitizers.scrubber import contains_pii
from tools.orchestrator_tools.read_tools import make_read_tools

log = logging.getLogger("orchestrator")
RECURSION_LIMIT = 16
GAVE_UP_REPLY = "I couldn't finish that request. Please try a simpler question."


class ChatRequest(BaseModel):
    thread_id: str | None = None
    message: str = Field(min_length=1, max_length=4000)


def history_for_client(messages) -> list[dict]:
    """The redacted history: what the user and assistant said, plus tool names and whether each call worked.
    Tool results are left out, because they can hold (wrapped) customer note text."""
    out = []
    for message in messages:
        if isinstance(message, HumanMessage):
            out.append({"role": "user", "text": str(message.content)})
        elif isinstance(message, AIMessage):
            out.append({"role": "assistant", "text": str(message.content), "tool_calls": [call["name"] for call in message.tool_calls]})
        elif isinstance(message, ToolMessage):
            try:
                ok = json.loads(message.content).get("ok") is True
            except (ValueError, AttributeError):
                ok = False
            out.append({"role": "tool", "name": message.name, "ok": ok})
    return out


def create_app(llm=None, store_http: httpx.AsyncClient | None = None, a2a_http: httpx.AsyncClient | None = None,
               a2a_retry_delay: float = 0.5, db_path: str | None = None) -> FastAPI:
    """`llm`, `store_http` and `a2a_http` are injectable so tests can use the fake model and in-process services."""
    llm = llm or make_llm(max_tokens=1024)
    db_path = db_path or os.environ.get("DB_PATH", "./data/orchestrator.db")
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

    tools = make_read_tools(call_skill)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Open the SQLite file once: one connection for the checkpoints, one for thread ownership."""
        saver_conn, bindings_conn = await connect(db_path), await connect(db_path)
        try:
            saver = AsyncSqliteSaver(saver_conn)
            await saver.setup()
            app.state.bindings = ThreadBindings(bindings_conn)
            await app.state.bindings.setup()
            app.state.graph = build_graph(llm, tools, checkpointer=saver)
            yield
        finally:
            await saver_conn.close()
            await bindings_conn.close()

    app = FastAPI(title="ShopHub orchestrator", lifespan=lifespan)

    async def authenticate(authorization: str | None) -> dict:
        if not authorization:
            raise HTTPException(status_code=401, detail="Unauthorized")
        try:
            me = await store_auth.whoami(authorization)
        except StoreUnavailable:
            raise HTTPException(status_code=503, detail="Service unavailable")
        if me is None:  # invalid token, or a token with no merchant
            raise HTTPException(status_code=401, detail="Unauthorized")
        return me

    async def require_own_thread(thread_id: str, merchant: str) -> None:
        """Same 404 for an unknown thread and for another merchant's thread."""
        if not await app.state.bindings.is_owned_by(thread_id, merchant):
            raise HTTPException(status_code=404, detail="Thread not found")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.post("/chat")
    async def chat(body: ChatRequest, authorization: str | None = Header(default=None)):
        me = await authenticate(authorization)
        if body.thread_id:
            await require_own_thread(body.thread_id, me["merchant"])
        if contains_pii(body.message):
            # Refuse here, before the graph runs, so the typed email or phone is never saved in a checkpoint.
            return {"thread_id": body.thread_id, "reply": PII_REFUSAL}
        thread_id = body.thread_id
        if not thread_id:
            thread_id = str(uuid.uuid4())
            await app.state.bindings.create(thread_id, me["merchant"])

        auth_token, merchant_token = current_auth.set(authorization), current_merchant.set(me["merchant"])
        try:
            # Only thread_id goes in `configurable`: LangGraph copies that dict into checkpoint metadata.
            config = {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}
            result = await app.state.graph.ainvoke({"messages": [HumanMessage(body.message)]}, config)
            last = result["messages"][-1]
            reply = last.content if isinstance(last, AIMessage) else GAVE_UP_REPLY
        except GraphRecursionError:
            reply = GAVE_UP_REPLY
        finally:
            current_auth.reset(auth_token)
            current_merchant.reset(merchant_token)
        return {"thread_id": thread_id, "reply": reply}

    @app.get("/threads/{thread_id}")
    async def get_thread(thread_id: str, authorization: str | None = Header(default=None)):
        me = await authenticate(authorization)
        await require_own_thread(thread_id, me["merchant"])
        state = await app.state.graph.aget_state({"configurable": {"thread_id": thread_id}})
        return {"thread_id": thread_id, "messages": history_for_client(state.values.get("messages", []))}

    return app
