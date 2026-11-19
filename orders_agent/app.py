"""Orders agent: a deterministic A2A service (no LLM) that owns the order skills.

Run:  uvicorn --factory orders_agent.app:create_app --port 8002
One JSON-RPC method (`SendMessage`), one skill per request. The token lives only in the
Authorization header: a middleware verifies it, and the executor forwards the exact header
value to the store on every call. Nothing about the token is stored (no Task is ever created).
"""
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from a2a.helpers import get_data_parts, new_data_message
from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import add_a2a_routes_to_fastapi, create_agent_card_routes, create_jsonrpc_routes
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities, AgentCard, AgentInterface, AgentSkill, HTTPAuthSecurityScheme, SecurityRequirement,
    SecurityScheme, StringList,
)
from a2a.utils.errors import InvalidParamsError
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from shared.contracts import (
    A2ASkillCall, Failure, FindOrdersByCustomerNameArgs, GetOrderArgs, GetOrderNoteArgs, ListOrdersArgs,
)
from tools.subagent_tools import read_skills, write_skills
from tools.subagent_tools.store_client import StoreClient, UpstreamUnavailable

# skill id -> (function, argument model, card description)
READ = {
    "list_orders": (read_skills.list_orders, ListOrdersArgs,
                    "List orders. args: {status?: PLACED|PAID|SHIPPED|DELIVERED|CANCELLED|REFUNDED, older_than_days?: int}"),
    "get_order": (read_skills.get_order, GetOrderArgs, "Get one order. args: {order_id: int}"),
    "find_orders_by_customer_name": (read_skills.find_orders_by_customer_name, FindOrdersByCustomerNameArgs,
                                     "Find orders by customer name (search filter only). args: {name: str}"),
    "get_order_note": (read_skills.get_order_note, GetOrderNoteArgs,
                       "Read the customer note of an order, scrubbed and wrapped as untrusted data. args: {order_id: int}"),
}
WRITE = {
    "refund_order": (write_skills.refund_order, GetOrderArgs, "Refund an order. Approve path only. args: {order_id: int}"),
    "cancel_order": (write_skills.cancel_order, GetOrderArgs, "Cancel an order. Approve path only. args: {order_id: int}"),
}
SKILLS = {**READ, **WRITE}


def build_card(public_url: str) -> AgentCard:
    return AgentCard(
        name="ShopHub orders agent",
        description="Reads and changes orders for the caller's merchant. Deterministic: no LLM.",
        version="0.1.0",
        supported_interfaces=[AgentInterface(url=public_url, protocol_binding="JSONRPC", protocol_version="1.0")],
        capabilities=AgentCapabilities(),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=[AgentSkill(id=name, name=name, description=desc, tags=["orders"]) for name, (_, _, desc) in SKILLS.items()],
        security_schemes={"bearer": SecurityScheme(http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="bearer"))},
        security_requirements=[SecurityRequirement(schemes={"bearer": StringList()})],
    )


class HeaderContextBuilder(DefaultServerCallContextBuilder):
    """Copies what the middleware verified into the per-call context (not persisted anywhere)."""

    def build(self, request):
        context = super().build(request)
        context.state["auth"] = getattr(request.state, "auth", None)
        context.state["merchant"] = getattr(request.state, "merchant", None)
        return context


class OrdersExecutor(AgentExecutor):
    def __init__(self, store: StoreClient, clock):
        self.store = store
        self.clock = clock

    async def execute(self, context, event_queue) -> None:
        state = context.call_context.state
        auth, merchant = state.get("auth"), state.get("merchant")
        if not auth or not merchant:  # the middleware makes this unreachable; fail closed anyway
            raise InvalidParamsError("unauthenticated")

        # Protocol faults (raised): not exactly one data part, malformed call, unknown skill.
        parts = get_data_parts(context.message.parts)
        if len(parts) != 1:
            raise InvalidParamsError("exactly one data part {skill, args} is required")
        try:
            call = A2ASkillCall.model_validate(parts[0])
        except ValidationError:
            raise InvalidParamsError("malformed skill call")
        if call.skill not in SKILLS:
            raise InvalidParamsError("unknown skill")

        # Business outcomes (returned as data).
        if call.merchant_claim is not None and call.merchant_claim != merchant:
            result = Failure(code="merchant_mismatch").model_dump()  # the token's merchant wins; nothing runs
        else:
            function, args_model, _ = SKILLS[call.skill]
            try:
                args = args_model.model_validate(call.args)
            except ValidationError:
                result = Failure(code="invalid_args").model_dump()
            else:
                if call.skill in WRITE:
                    result = await function(self.store, auth, args.order_id)
                else:
                    result = await function(self.store, auth, args, self.clock())
        await event_queue.enqueue_event(new_data_message(result))

    async def cancel(self, context, event_queue) -> None:
        raise InvalidParamsError("cancel is not supported")


def create_app(store_url: str | None = None, http_client=None, clock=None) -> FastAPI:
    """`http_client` and `clock` exist so tests can wire the real store app and a fixed time."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    owns_client = http_client is None
    store = StoreClient(http_client) if http_client else StoreClient.from_url(
        store_url or os.environ.get("STORE_URL", "http://localhost:8001"))
    card = build_card(os.environ.get("A2A_PUBLIC_URL", "http://localhost:8002"))
    task_store = InMemoryTaskStore()  # stays empty: we answer with messages, never tasks
    handler = DefaultRequestHandler(agent_executor=OrdersExecutor(store, clock), task_store=task_store, agent_card=card)

    @asynccontextmanager
    async def lifespan(app):
        yield
        if owns_client:
            await store.aclose()

    app = FastAPI(title="ShopHub orders agent", lifespan=lifespan)
    app.state.task_store = task_store  # exposed so a test can prove it holds no token

    @app.middleware("http")
    async def require_valid_token(request: Request, call_next):
        # Only the A2A endpoint is protected. The agent card stays public.
        if request.method == "POST" and request.url.path == "/":
            auth = request.headers.get("authorization")
            if not auth:
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
            try:
                me = await store.me(auth)
            except UpstreamUnavailable:
                return JSONResponse({"detail": "Store unavailable"}, status_code=503)
            if me is None or me["merchant"] is None:  # invalid token, or a token with no merchant
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
            request.state.auth = auth
            request.state.merchant = me["merchant"]
        return await call_next(request)

    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/", context_builder=HeaderContextBuilder()),
    )
    return app
