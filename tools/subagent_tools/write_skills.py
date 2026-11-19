"""Write skills (refund_order, cancel_order). Called only from the approve path, never bound to the model.

Writes are never retried. Business outcomes are returned as data.
"""
from shared.contracts import WriteOutcome
from tools.subagent_tools.store_client import StoreClient, UpstreamUnavailable


async def _write(call, auth: str, order_id: int) -> dict:
    try:
        status_code, body = await call(auth, order_id)
    except UpstreamUnavailable:
        return WriteOutcome(ok=False, code="UPSTREAM_UNAVAILABLE", order_id=order_id).model_dump()
    if status_code == 200:
        return WriteOutcome(ok=True, code="OK", order_id=order_id, current_status=body["status"]).model_dump()
    if status_code == 404:  # unknown and foreign ids look the same
        return WriteOutcome(ok=False, code="NOT_FOUND_OR_NOT_ACCESSIBLE", order_id=order_id).model_dump()
    if status_code == 409:
        return WriteOutcome(ok=False, code="INVALID_STATE", order_id=order_id,
                            current_status=body["detail"]["current_status"]).model_dump()
    return WriteOutcome(ok=False, code="UPSTREAM_UNAVAILABLE", order_id=order_id).model_dump()


async def refund_order(store: StoreClient, auth: str, order_id: int) -> dict:
    return await _write(store.refund, auth, order_id)


async def cancel_order(store: StoreClient, auth: str, order_id: int) -> dict:
    return await _write(store.cancel, auth, order_id)
