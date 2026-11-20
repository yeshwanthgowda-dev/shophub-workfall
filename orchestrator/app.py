"""Chat API (plain JSON, one turn per call; streaming comes in module 6). Conversations persist in SQLite (module 4).

Run:  uvicorn --factory orchestrator.app:create_app --port 8000
"""
import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from pydantic import BaseModel, Field

from orchestrator.a2a_client import A2AClient, AgentUnavailable
from orchestrator.auth import StoreAuth, StoreUnavailable
from orchestrator.context import current_auth, current_merchant
from orchestrator.db import ThreadBindings, connect
from orchestrator.graph import PII_REFUSAL, build_graph
from orchestrator.llm import make_llm
from orchestrator.proposals import Proposals
from orchestrator.streaming import graph_events, sse
from orchestrator.verifier import make_verifier
from sanitizers.scrubber import contains_pii
from tools.orchestrator_tools.propose_tools import make_propose_tools
from tools.orchestrator_tools.read_tools import make_read_tools

log = logging.getLogger("orchestrator")
RECURSION_LIMIT = 16
HEARTBEAT_SECONDS = 15.0
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


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
               a2a_retry_delay: float = 0.5, db_path: str | None = None, verifier=None, clock=None,
               proposal_ttl_seconds: int | None = None) -> FastAPI:
    """`llm`, `store_http`, `a2a_http`, `verifier` and `clock` are injectable so tests can use fakes and in-process services."""
    llm = llm or make_llm(max_tokens=1024)
    verifier = verifier or make_verifier()
    clock = clock or (lambda: datetime.now(timezone.utc))
    ttl = proposal_ttl_seconds or int(os.environ.get("PROPOSAL_TTL_SECONDS", "900"))
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
            app.state.proposals = Proposals(bindings_conn, clock, ttl)
            await app.state.proposals.setup()
            app.state.graph = build_graph(llm, tools, checkpointer=saver, propose_tools=make_propose_tools(),
                                          proposals=app.state.proposals, verifier=verifier, call_skill=call_skill)
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
        """One turn as a stream of server-sent events. Everything that can fail with a status code happens before it starts."""
        me = await authenticate(authorization)
        if body.thread_id:
            await require_own_thread(body.thread_id, me["merchant"])
        if contains_pii(body.message):
            # Refuse here, before the graph runs, so the typed email or phone is never saved in a checkpoint.
            async def refusal():
                yield sse("token", {"text": PII_REFUSAL})
                yield sse("done", {"thread_id": body.thread_id, "usage": {}})
            return StreamingResponse(refusal(), media_type="text/event-stream", headers=SSE_HEADERS)
        thread_id = body.thread_id
        if not thread_id:
            thread_id = str(uuid.uuid4())
            await app.state.bindings.create(thread_id, me["merchant"])
        await app.state.proposals.supersede_pending(thread_id)  # a new message replaces a proposal still waiting

        # Only thread_id goes in `configurable`: LangGraph copies that dict into checkpoint metadata.
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}
        graph_input = {"messages": [HumanMessage(body.message)]}

        async def produce(queue: asyncio.Queue):
            # The caller's identity is set here, inside the task: the handler has returned by the time the stream runs.
            current_auth.set(authorization)
            current_merchant.set(me["merchant"])
            try:
                async for event in graph_events(app.state.graph, graph_input, config, app.state.proposals, thread_id):
                    await queue.put(event)
            finally:
                await queue.put(None)

        async def stream():
            queue: asyncio.Queue = asyncio.Queue()
            task = asyncio.create_task(produce(queue))
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"  # keeps proxies from closing a quiet connection
                        continue
                    if item is None:
                        break
                    yield sse(*item)
            finally:
                task.cancel()  # the client went away (or we are done): stop the run

        return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)

    async def decide(thread_id: str, proposal_id: str, authorization: str | None, decision: str):
        """Approve or reject. The approver's own token is checked, and is the one used to execute."""
        me = await authenticate(authorization)
        await require_own_thread(thread_id, me["merchant"])
        row = await app.state.proposals.get(proposal_id)
        if row is None or row["thread_id"] != thread_id or row["merchant"] != me["merchant"]:
            raise HTTPException(status_code=404, detail="Proposal not found")  # unknown, wrong thread and foreign look alike
        proposals = app.state.proposals
        if not await proposals.claim(proposal_id, "approved" if decision == "approve" else "rejected"):
            return JSONResponse({"code": await proposals.why_not_claimable(proposal_id)}, status_code=409)

        auth_token, merchant_token = current_auth.set(authorization), current_merchant.set(me["merchant"])
        try:
            await app.state.graph.ainvoke(Command(resume={"decision": decision}), {"configurable": {"thread_id": thread_id}})
        except Exception:
            log.exception("resume failed")
        finally:
            current_auth.reset(auth_token)
            current_merchant.reset(merchant_token)
        await proposals.finish(proposal_id, "failed", "UPSTREAM_UNAVAILABLE")  # only changes a row still stuck at 'approved'
        row = await proposals.get(proposal_id)
        return {"proposal_id": proposal_id, "status": row["status"], "outcome": row["outcome"], "current_status": row["current_status"]}

    @app.post("/threads/{thread_id}/proposals/{proposal_id}/approve")
    async def approve(thread_id: str, proposal_id: str, authorization: str | None = Header(default=None)):
        return await decide(thread_id, proposal_id, authorization, "approve")

    @app.post("/threads/{thread_id}/proposals/{proposal_id}/reject")
    async def reject(thread_id: str, proposal_id: str, authorization: str | None = Header(default=None)):
        return await decide(thread_id, proposal_id, authorization, "reject")

    @app.get("/threads/{thread_id}")
    async def get_thread(thread_id: str, authorization: str | None = Header(default=None)):
        me = await authenticate(authorization)
        await require_own_thread(thread_id, me["merchant"])
        state = await app.state.graph.aget_state({"configurable": {"thread_id": thread_id}})
        return {"thread_id": thread_id, "messages": history_for_client(state.values.get("messages", []))}

    return app
