"""Read skills of the orders agent. Each returns a plain dict that is safe to send to the orchestrator.

Every order leaves through `build_order_payload`, so no email, phone, customer name or note text
can appear. Business failures are returned as data (`ok: false`), never raised.
"""
from datetime import datetime, timedelta

from sanitizers.untrusted import prepare_note
from shared.contracts import (
    CustomerMatch, Failure, FindOrdersByCustomerNameArgs, FindOrdersByCustomerNameResult, GetOrderArgs,
    GetOrderNoteArgs, GetOrderNoteResult, GetOrderResult, ListOrdersArgs, ListOrdersResult,
)
from tools.subagent_tools.order_payload import build_order_payload
from tools.subagent_tools.store_client import StoreClient, UpstreamUnavailable


def _older_than(placed_at: str, days: int, now: datetime) -> bool:
    return datetime.fromisoformat(placed_at.replace("Z", "+00:00")) <= now - timedelta(days=days)


async def list_orders(store: StoreClient, auth: str, args: ListOrdersArgs, now: datetime) -> dict:
    try:
        orders = await store.list_orders(auth, args.status)
    except UpstreamUnavailable:
        return Failure(code="upstream_unavailable").model_dump()
    if args.older_than_days is not None:
        orders = [o for o in orders if _older_than(o["placed_at"], args.older_than_days, now)]
    return ListOrdersResult(orders=[build_order_payload(o) for o in orders]).model_dump()


async def get_order(store: StoreClient, auth: str, args: GetOrderArgs, now: datetime) -> dict:
    try:
        order = await store.lookup(auth, args.order_id)
    except UpstreamUnavailable:
        return Failure(code="upstream_unavailable").model_dump()
    if order is None:  # unknown and foreign ids look the same
        return Failure(code="not_found").model_dump()
    return GetOrderResult(order=build_order_payload(order)).model_dump()


async def find_orders_by_customer_name(store: StoreClient, auth: str, args: FindOrdersByCustomerNameArgs,
                                       now: datetime) -> dict:
    """The name is only a search filter here. It is never returned."""
    try:
        orders = await store.list_orders(auth)
    except UpstreamUnavailable:
        return Failure(code="upstream_unavailable").model_dump()
    wanted = args.name.lower()
    by_customer: dict[tuple[str, str], list[dict]] = {}
    for order in orders:
        if wanted in order["customer_name"].lower():
            by_customer.setdefault((order["customer_name"], order["customer_email"]), []).append(order)
    several = len(by_customer) > 1  # only label customers when they must be told apart
    matches = [
        CustomerMatch(
            customer_label=f"customer #{i}" if several else None,
            orders=[build_order_payload(o) for o in by_customer[key]],
        )
        for i, key in enumerate(sorted(by_customer), start=1)  # sorted: labels stay stable between calls
    ]
    return FindOrdersByCustomerNameResult(matches=matches).model_dump()


async def get_order_note(store: StoreClient, auth: str, args: GetOrderNoteArgs, now: datetime) -> dict:
    """The only skill that returns note text: scrubbed, wrapped as untrusted, and flagged."""
    try:
        order = await store.lookup(auth, args.order_id)
    except UpstreamUnavailable:
        return Failure(code="upstream_unavailable").model_dump()
    if order is None:
        return Failure(code="not_found").model_dump()
    raw_note = order.get("customer_note") or ""
    if not raw_note.strip():
        return GetOrderNoteResult(order_id=args.order_id, note="", note_flagged=False).model_dump()
    note, flagged = prepare_note(raw_note)
    return GetOrderNoteResult(order_id=args.order_id, note=note, note_flagged=flagged).model_dump()
