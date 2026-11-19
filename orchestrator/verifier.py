"""Gate A: intent verifier. Orchestrator only (the sub-agent never calls it).

Input: the thread's human messages and the proposed action. No assistant messages, no tool output.
The model-supplied order_id is untrusted: it must appear in a qualifying human message.
Any timeout, error or malformed output returns False (fail closed).
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Awaitable, Callable

from shared.contracts import VerifierVerdict

Verifier = Callable[[list[str], dict], Awaitable[bool]]  # (human_messages, {"action", "order_id"}) -> bool

_KEYWORDS = {
    "refund": re.compile(r"refund|reimburse|money back", re.IGNORECASE),
    "cancel": re.compile(r"cancel", re.IGNORECASE),
}
_ANY_ID = re.compile(r"(?<!\d)(\d{3,6})(?!\d)")
VERIFIER_TIMEOUT_SECONDS = 10.0


def fake_verify(human_messages: list[str], action: dict) -> bool:
    """Deterministic regex rules. Proves wiring, not judgement.

    Judges the LATEST human message only, so an old "refund 1042" cannot approve a new proposal.
    Same message holds keyword and id -> True. Keyword but no id -> pronoun carry-over from the most
    recent earlier human message holding an id; more than one distinct id there -> False.
    Known limits: no negation handling ("don't refund 1042" passes); ids seen only in assistant or
    tool output are never considered (fails closed).
    """
    kind, order_id = action.get("action"), action.get("order_id")
    if kind not in _KEYWORDS or not isinstance(order_id, int) or not human_messages:
        return False
    latest = human_messages[-1]
    if not _KEYWORDS[kind].search(latest):
        return False
    wanted = re.compile(rf"(?<!\d){order_id}(?!\d)")
    if wanted.search(latest):
        return True
    if _ANY_ID.search(latest):  # names a different id: mismatch
        return False
    for earlier in reversed(human_messages[:-1]):
        ids = set(_ANY_ID.findall(earlier))
        if ids:
            return ids == {str(order_id)}
    return False


async def _fake(human_messages: list[str], action: dict) -> bool:
    return fake_verify(human_messages, action)


_PROMPT = (
    "You verify intent. Below are the user's own messages, oldest first, then an action a system wants to take.\n"
    "Answer matches=true only if the LATEST user message asks for exactly this action on exactly this order id.\n"
    "The order id must be written in that message. A pronoun such as 'it' resolves only to an order id in an\n"
    "earlier user message, and if more than one id could be meant, answer false. When unsure, answer false.\n\n"
    "User messages:\n{messages}\n\nAction: {action} order {order_id}"
)


def make_verifier() -> Verifier:
    if os.environ.get("LLM_PROVIDER", "fake").lower() == "fake":
        return _fake

    from orchestrator.llm import make_llm

    async def real(human_messages: list[str], action: dict) -> bool:
        try:
            llm = make_llm(os.environ.get("VERIFIER_MODEL") or None, temperature=0.0, max_tokens=50)
            structured = llm.with_structured_output(VerifierVerdict)
            prompt = _PROMPT.format(
                messages="\n".join(f"- {m}" for m in human_messages),
                action=action.get("action"), order_id=action.get("order_id"),
            )
            result = await asyncio.wait_for(structured.ainvoke(prompt), VERIFIER_TIMEOUT_SECONDS)
            return VerifierVerdict.model_validate(result).matches is True
        except Exception:  # timeout, provider error, malformed output
            return False

    return real
