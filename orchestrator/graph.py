"""The chat graph: pre_model_guard -> agent <-> tools. Hand-built so every step is readable.

State holds only messages. No token, no merchant credentials, no PII ever enters it.
"""
import logging
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import ValidationError

from orchestrator.prompts import SYSTEM_PROMPT
from sanitizers.scrubber import contains_pii
from shared.contracts import Failure

log = logging.getLogger("orchestrator")

PII_REFUSAL = ("I can't work with messages that contain email addresses or phone numbers. "
               "Please remove them and ask again using an order id or a customer's name.")
TOOL_LIMIT_REPLY = "I've reached the tool call limit for this question. Please ask a narrower question."


class ChatState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


def tool_calls_this_turn(messages: list[BaseMessage]) -> int:
    """Number of tool results since the latest human message."""
    count = 0
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            break
        if isinstance(message, ToolMessage):
            count += 1
    return count


def build_graph(llm, tools, checkpointer=None, max_tool_calls: int = 6):
    model = llm.bind_tools(tools)
    tools_by_name = {tool.name: tool for tool in tools}

    async def pre_model_guard(state: ChatState) -> dict:
        """User-typed email or phone never reaches the model: answer with a fixed refusal."""
        last = state["messages"][-1]
        if isinstance(last, HumanMessage) and contains_pii(str(last.content)):
            return {"messages": [AIMessage(PII_REFUSAL)]}
        return {}

    def after_guard(state: ChatState) -> str:
        return END if isinstance(state["messages"][-1], AIMessage) else "agent"

    async def agent(state: ChatState) -> dict:
        if tool_calls_this_turn(state["messages"]) >= max_tool_calls:
            return {"messages": [AIMessage(TOOL_LIMIT_REPLY)]}
        reply = await model.ainvoke([SystemMessage(SYSTEM_PROMPT)] + state["messages"])
        return {"messages": [reply]}

    def after_agent(state: ChatState) -> str:
        last = state["messages"][-1]
        return "tools" if isinstance(last, AIMessage) and last.tool_calls else END

    async def tools_node(state: ChatState) -> dict:
        results = []
        for call in state["messages"][-1].tool_calls:
            tool = tools_by_name.get(call["name"])
            if tool is None:  # not one of our read tools, for example refund_order
                content = Failure(code="unknown_tool").model_dump_json()
            else:
                try:
                    content = await tool.ainvoke(call["args"])
                except ValidationError:
                    content = Failure(code="invalid_args").model_dump_json()
                except Exception:
                    log.exception("tool %s failed", call["name"])
                    content = Failure(code="tool_error").model_dump_json()
            results.append(ToolMessage(content=content, tool_call_id=call["id"], name=call["name"]))
        return {"messages": results}

    graph = StateGraph(ChatState)
    graph.add_node("pre_model_guard", pre_model_guard)
    graph.add_node("agent", agent)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "pre_model_guard")
    graph.add_conditional_edges("pre_model_guard", after_guard, ["agent", END])
    graph.add_conditional_edges("agent", after_agent, ["tools", END])
    graph.add_edge("tools", "agent")
    return graph.compile(checkpointer=checkpointer)
