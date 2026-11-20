"""Turns one graph run into SSE events: token, tool_started, tool_finished, proposal, error, done.

Nothing unredacted is streamed. Tokens come from the model (which never sees PII) or from fixed templates.
tool_finished carries only the tool name, its call id and whether it worked, never the tool's result.
"""
import json
import logging

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langgraph.errors import GraphRecursionError

log = logging.getLogger("orchestrator")
GAVE_UP_REPLY = "I couldn't finish that request. Please try a simpler question."


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def proposal_for_client(row: dict) -> dict:
    return {
        "proposal_id": row["id"], "order_id": row["order_id"], "merchant": row["merchant"], "action": row["action"],
        "amount": row["amount"], "from_status": row["from_status"], "to_status": row["to_status"],
        "expires_at": row["expires_at"], "note_flagged": bool(row["note_flagged"]),
    }


def tool_succeeded(message: ToolMessage) -> bool:
    try:
        return json.loads(message.content).get("ok") is True
    except (ValueError, AttributeError):
        return False


async def graph_events(graph, graph_input, config: dict, proposals, thread_id: str | None):
    """Yield (event name, data) for one run. The last one is always `done` or `error`."""
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    streamed = ""  # text already sent as tokens by the model call that is currently running
    try:
        async for mode, chunk in graph.astream(graph_input, config, stream_mode=["messages", "updates"]):
            if mode == "messages":
                message, meta = chunk
                if meta.get("langgraph_node") == "agent" and isinstance(message, AIMessageChunk) and message.content:
                    streamed += message.content
                    yield "token", {"text": message.content}
                continue
            for node, update in chunk.items():
                if node == "__interrupt__":  # paused for approval
                    row = await proposals.get(update[0].value["proposal_id"])
                    yield "proposal", proposal_for_client(row)
                    continue
                for message in (update or {}).get("messages", []):
                    if isinstance(message, ToolMessage):
                        yield "tool_finished", {"name": message.name, "call_id": message.tool_call_id, "ok": tool_succeeded(message)}
                    elif isinstance(message, AIMessage):
                        for call in message.tool_calls:
                            yield "tool_started", {"name": call["name"], "call_id": call["id"]}
                        for key in usage:
                            usage[key] += (message.usage_metadata or {}).get(key, 0)
                        if message.content and message.content != streamed:  # fixed template: the model did not stream it
                            yield "token", {"text": message.content}
                        streamed = ""
    except GraphRecursionError:
        yield "token", {"text": GAVE_UP_REPLY}
    except Exception:
        log.exception("chat run failed")
        yield "error", {"code": "internal_error"}
        return
    yield "done", {"thread_id": thread_id, "usage": usage}
