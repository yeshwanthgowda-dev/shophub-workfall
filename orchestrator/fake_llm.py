"""Scripted fake chat model. Picks its next action by rule from message history, not a queue.

Why rules: a queue breaks on retries, resume after an interrupt and reordered calls.
A rule reads the same history and gives the same answer every time.

Gullible mode: obeys instruction-like text found in tool messages. It proves the
deterministic layers (allowlist, proposals, approval) hold when the model is fooled.
It does NOT prove a real model resists injection.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, AsyncIterator, Iterator, Sequence

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field

ID_RE = re.compile(r"(?<!\d)(\d{3,6})(?!\d)")
REFUND_RE = re.compile(r"refund|reimburse|money back", re.IGNORECASE)
CANCEL_RE = re.compile(r"cancel", re.IGNORECASE)
NOTE_RE = re.compile(r"\bnote\b|deliver|why|reason|request", re.IGNORECASE)
INJECTION_RE = re.compile(
    r"ignore\b.{0,40}\binstructions|^\s*(?:assistant|system)\s*:|\b(?:refund|cancel)\b.{0,30}\b(?:immediately|now|this order)\b",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
STATUSES = ("PLACED", "PAID", "SHIPPED", "DELIVERED", "CANCELLED", "REFUNDED")
NAME_RE = re.compile(r"(?:what did|orders? (?:for|from|by)|customer)\s+([A-Z][a-zA-Z'\-]+)")
DAYS_RE = re.compile(r"(\d+)\s*days?", re.IGNORECASE)
UNSHIPPED_RE = re.compile(r"not\s+(?:been\s+)?shipped|unshipped", re.IGNORECASE)


def _make_tool_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "args": args, "id": f"call_{uuid.uuid4().hex[:12]}", "type": "tool_call"}


def _words(text: str) -> int:
    return len(text.split())


class FakeChatModel(BaseChatModel):
    gullible: bool = False
    gullible_tool: str = "refund_order"  # what a fooled model reaches for; refund_order is never bound
    bound_tools: list[str] = Field(default_factory=list)
    calls: list[list[BaseMessage]] = Field(default_factory=list)  # every batch received (shared by bind_tools copies)

    @property
    def _llm_type(self) -> str:
        return "shophub-fake"

    def bind_tools(self, tools: Sequence[Any], *, tool_choice: Any = None, **kwargs: Any):
        names = [convert_to_openai_tool(t)["function"]["name"] for t in tools]
        return self.model_copy(update={"bound_tools": names})  # shallow copy: `calls` stays shared

    # ----- rule engine -----

    def _tool_accessible(self, tool: str) -> bool:
        return tool in self.bound_tools

    def _choose_reply(self, messages: list[BaseMessage]) -> AIMessage:
        self.calls.append(list(messages))
        last = messages[-1] if messages else None
        if isinstance(last, ToolMessage):
            return self._reply_to_tool_result(messages)
        if isinstance(last, HumanMessage):
            return self._reply_to_human(messages)
        return AIMessage(content="How can I help with your orders?")

    def _human_texts(self, messages: list[BaseMessage]) -> list[str]:
        return [m.content for m in messages if isinstance(m, HumanMessage) and isinstance(m.content, str)]

    def _reply_to_human(self, messages: list[BaseMessage]) -> AIMessage:
        humans = self._human_texts(messages)
        text = humans[-1]
        order_id = self._find_order_id(humans)
        for rx, tool in ((REFUND_RE, "propose_refund"), (CANCEL_RE, "propose_cancel")):
            if rx.search(text):
                if order_id and self._tool_accessible(tool):
                    return self._tool_call_message([_make_tool_call(tool, {"order_id": order_id})])
                return AIMessage(content="Which order id should I use? Please include the order number.")
        if order_id and NOTE_RE.search(text) and self._tool_accessible("get_order_note"):
            return self._tool_call_message([_make_tool_call("get_order_note", {"order_id": order_id})])
        if order_id and self._tool_accessible("get_order"):
            return self._tool_call_message([_make_tool_call("get_order", {"order_id": order_id})])
        match = NAME_RE.search(text)
        if match and self._tool_accessible("find_orders_by_customer_name"):
            return self._tool_call_message([_make_tool_call("find_orders_by_customer_name", {"name": match.group(1)})])
        if self._tool_accessible("list_orders") and re.search(r"\border|shipped|paid|placed|delivered|cancelled|refunded", text, re.IGNORECASE):
            args: dict[str, Any] = {}
            status = next((s for s in STATUSES if s.lower() in text.lower()), None)
            if UNSHIPPED_RE.search(text):
                status = status or "PAID"
            if status:
                args["status"] = status
            days = DAYS_RE.search(text)
            if days:
                args["older_than_days"] = int(days.group(1))
            return self._tool_call_message([_make_tool_call("list_orders", args)])
        return AIMessage(content="I can help with orders. Ask about an order id, a status, or a customer name.")

    def _find_order_id(self, humans: list[str]) -> int | None:
        """Id in the latest human message, else carried over from the most recent earlier one."""
        for text in reversed(humans):
            found = ID_RE.findall(text)
            if found:
                return int(found[0])
        return None

    def _reply_to_tool_result(self, messages: list[BaseMessage]) -> AIMessage:
        tool_msgs: list[ToolMessage] = []
        for m in reversed(messages):
            if not isinstance(m, ToolMessage):
                break
            tool_msgs.append(m)
        tool_msgs.reverse()
        content = "\n".join(str(m.content) for m in tool_msgs)
        if self.gullible and INJECTION_RE.search(content):
            order_id = self._find_order_id_in_tool_output(content)
            if order_id is not None:
                return self._tool_call_message([_make_tool_call(self.gullible_tool, {"order_id": order_id})])
        humans = self._human_texts(messages)
        if re.search(r'"code"\s*:\s*"not_found"', content):  # tell the user something useful
            order_id = self._find_order_id(humans)
            return AIMessage(content=f"I couldn't find order {order_id} in your store. "
                                     "You could check the number, list recent orders, or search by customer name.")
        # A note exists and the question touches it: read it once per turn.
        called = {tc["name"] for m in messages if isinstance(m, AIMessage) for tc in m.tool_calls}
        if (
            re.search(r'"has_note"\s*:\s*true', content)
            and "get_order_note" not in called
            and humans and NOTE_RE.search(humans[-1])
            and self._tool_accessible("get_order_note")
        ):
            order_id = self._find_order_id_in_tool_output(content)
            if order_id is not None:
                return self._tool_call_message([_make_tool_call("get_order_note", {"order_id": order_id})])
        return AIMessage(content=f"Here is what I found: {content[:500]}")

    @staticmethod
    def _find_order_id_in_tool_output(content: str) -> int | None:
        for rx in (r'"order_id"\s*:\s*(\d+)', r'"id"\s*:\s*(\d+)'):
            m = re.search(rx, content)
            if m:
                return int(m.group(1))
        m = ID_RE.search(content)
        return int(m.group(1)) if m else None

    def _tool_call_message(self, tool_calls: list[dict[str, Any]]) -> AIMessage:
        return AIMessage(content="", tool_calls=tool_calls)

    @staticmethod
    def _add_usage(msg: AIMessage, messages: list[BaseMessage]) -> AIMessage:
        inp = sum(_words(str(m.content)) for m in messages)
        out = _words(str(msg.content)) + 5 * len(msg.tool_calls)
        msg.usage_metadata = {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}
        return msg

    # ----- BaseChatModel plumbing -----

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                  run_manager: CallbackManagerForLLMRun | None = None, **kwargs: Any) -> ChatResult:
        msg = self._add_usage(self._choose_reply(messages), messages)
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _chunks(self, messages: list[BaseMessage]) -> Iterator[ChatGenerationChunk]:
        msg = self._add_usage(self._choose_reply(messages), messages)
        if msg.tool_calls:
            for i, tc in enumerate(msg.tool_calls):
                yield ChatGenerationChunk(message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[{"name": tc["name"], "args": json.dumps(tc["args"]), "id": tc["id"], "index": i}],
                ))
        else:
            for piece in re.findall(r"\S+\s*", str(msg.content)):
                yield ChatGenerationChunk(message=AIMessageChunk(content=piece))
        yield ChatGenerationChunk(message=AIMessageChunk(content="", usage_metadata=msg.usage_metadata))

    def _stream(self, messages: list[BaseMessage], stop: list[str] | None = None,
                run_manager: CallbackManagerForLLMRun | None = None, **kwargs: Any) -> Iterator[ChatGenerationChunk]:
        yield from self._chunks(messages)

    async def _astream(self, messages: list[BaseMessage], stop: list[str] | None = None,
                       run_manager: AsyncCallbackManagerForLLMRun | None = None, **kwargs: Any) -> AsyncIterator[ChatGenerationChunk]:
        for chunk in self._chunks(messages):
            yield chunk
