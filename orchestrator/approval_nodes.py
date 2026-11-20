"""The three graph nodes of the approval flow: propose -> approval -> execute.

propose   checks intent (Gate A), looks the order up, and saves a pending proposal. Moves no money.
approval  only calls interrupt(). It has no side effects, because a resumed node runs again from its top.
execute   runs after a human approved (Gate B). It uses the approver's token, taken from the request context.
"""
import logging

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt
from pydantic import ValidationError

from orchestrator.context import current_merchant
from shared.contracts import Failure, GetOrderResult, ProposeArgs, PROPOSE_TOOLS, WriteOutcome

log = logging.getLogger("orchestrator")

TARGET_STATUS = {"refund": "REFUNDED", "cancel": "CANCELLED"}
REFUSAL = "I could not confirm that you asked for this action. Please send a new message naming the action and the order id."
NOTE_WARNING = " The customer note on this order may contain instructions. Treat it as untrusted."
REJECTED_REPLY = "Okay, I did not go ahead with it."


def tool_reply(call: dict, body) -> ToolMessage:
    return ToolMessage(content=body.model_dump_json() if hasattr(body, "model_dump_json") else body, tool_call_id=call["id"], name=call["name"])


def make_approval_nodes(proposals, verifier, call_skill):
    async def propose_one(call: dict, state: dict, thread_id: str):
        """-> (tool result text, proposal id or None, refusal template or None)"""
        action = "refund" if call["name"] == "propose_refund" else "cancel"
        try:
            order_id = ProposeArgs.model_validate(call["args"]).order_id
        except ValidationError:
            return Failure(code="invalid_args").model_dump_json(), None, None

        # Gate A: the verifier sees the user's own messages only, never tool output or assistant text.
        humans = [str(m.content) for m in state["messages"] if isinstance(m, HumanMessage)]
        try:
            confirmed = await verifier(humans, {"action": action, "order_id": order_id})
        except Exception:
            log.exception("verifier failed")
            confirmed = False  # fail closed
        if not confirmed:
            template = REFUSAL + (NOTE_WARNING if state.get("note_read") else "")
            return Failure(code="intent_not_confirmed").model_dump_json(), None, template

        # The server, not the model, decides merchant, amount and statuses.
        try:
            data = await call_skill("get_order", {"order_id": order_id})
            order = GetOrderResult.model_validate(data).order if data.get("ok") is True else None
        except ValidationError:
            return Failure(code="invalid_agent_response").model_dump_json(), None, None
        if order is None:
            return Failure(code=data.get("code", "not_found")).model_dump_json(), None, None

        created, row = await proposals.create(
            thread_id=thread_id, tool_call_id=call["id"], merchant=current_merchant.get(), order_id=order_id, action=action,
            amount=order.total, from_status=order.status, to_status=TARGET_STATUS[action], note_flagged=bool(state.get("note_read")),
        )
        if not created:
            return f'{{"ok":false,"code":"proposal_already_pending","proposal_id":"{row["id"]}"}}', None, None
        body = f'{{"ok":true,"proposal_id":"{row["id"]}","order_id":{order_id},"action":"{action}","status":"pending"}}'
        return body, row["id"], None

    async def propose(state: dict, config: RunnableConfig) -> dict:
        thread_id = config["configurable"]["thread_id"]
        results, proposal_id, template = [], None, None
        already_proposed = bool(state.get("proposed"))
        for call in state["messages"][-1].tool_calls:
            if call["name"] not in PROPOSE_TOOLS:
                results.append(tool_reply(call, Failure(code="not_run")))  # every call needs an answer in the history
            elif already_proposed:  # one proposal per turn
                results.append(tool_reply(call, '{"ok":false,"code":"proposal_already_pending"}'))
            else:
                already_proposed = True
                text, proposal_id, template = await propose_one(call, state, thread_id)
                results.append(tool_reply(call, text))
        update = {"messages": results, "proposed": True}
        if proposal_id:
            update["proposal_id"] = proposal_id
        if template:
            update["messages"] = results + [AIMessage(template)]
        return update

    def after_propose(state: dict) -> str:
        if isinstance(state["messages"][-1], AIMessage):
            return "__end__"  # Gate A refused: the run ends with the fixed template
        return "approval" if state.get("proposal_id") else "agent"

    async def approval(state: dict) -> dict:
        answer = interrupt({"proposal_id": state["proposal_id"]})  # the run pauses here until /approve or /reject
        if answer.get("decision") == "approve":
            return {"decision": "approve"}
        return {"decision": "reject", "messages": [AIMessage(REJECTED_REPLY)]}

    def after_approval(state: dict) -> str:
        return "execute" if state.get("decision") == "approve" else "__end__"

    async def execute(state: dict) -> dict:
        row = await proposals.get(state["proposal_id"])
        action, order_id = row["action"], row["order_id"]
        outcome, current_status = await run_write(row)
        status = "executed" if outcome == "OK" else "failed"
        await proposals.finish(row["id"], status, outcome, current_status)
        if outcome == "OK":
            text = f"Done: the {action} for order {order_id} has been processed."
        elif outcome == "INVALID_STATE":
            text = f"I did not run it: order {order_id} is now {current_status}, so the {action} is no longer possible."
        else:
            text = f"I could not complete the {action} for order {order_id} ({outcome})."
        return {"messages": [AIMessage(text)]}

    async def run_write(row: dict) -> tuple[str, str | None]:
        """Re-read the order, compare it to the snapshot, then call the write skill. -> (outcome code, current status)"""
        try:
            data = await call_skill("get_order", {"order_id": row["order_id"]})
            if data.get("ok") is not True:
                return ("NOT_FOUND_OR_NOT_ACCESSIBLE" if data.get("code") == "not_found" else "UPSTREAM_UNAVAILABLE"), None
            current = GetOrderResult.model_validate(data).order.status
            if current != row["from_status"]:  # the order changed since the proposal was made
                return "INVALID_STATE", current
            result = WriteOutcome.model_validate(await call_skill(f"{row['action']}_order", {"order_id": row["order_id"]}))
        except Exception:
            log.exception("execute failed")
            return "UPSTREAM_UNAVAILABLE", None
        return result.code, result.current_status

    return propose, after_propose, approval, after_approval, execute
