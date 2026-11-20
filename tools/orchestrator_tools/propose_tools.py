"""propose_refund and propose_cancel as the model sees them: a name, a description and one argument.

These tools never run. The graph's `propose` node handles a call to them, because a proposal needs
the verifier, the database and the human approval step, none of which a tool should know about.
"""
from langchain_core.tools import StructuredTool

from shared.contracts import ProposeArgs


async def _never_runs(**kwargs) -> str:
    raise RuntimeError("propose tools are handled by the graph's propose node")


def make_propose_tools() -> list[StructuredTool]:
    return [
        StructuredTool.from_function(
            coroutine=_never_runs, name=name, args_schema=ProposeArgs,
            description=f"Ask a human to approve a {action} of one order. Call it only when the user's latest message "
                        f"asks for this {action} and names the order. It does not {action} anything by itself.",
        )
        for name, action in (("propose_refund", "refund"), ("propose_cancel", "cancel"))
    ]
