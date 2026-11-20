"""Per-request caller identity. Set by the HTTP handler, read when a tool calls the orders agent.

A ContextVar is used so the token never enters graph state, tool arguments or any checkpoint.
"""
from contextvars import ContextVar

current_auth: ContextVar[str | None] = ContextVar("current_auth", default=None)          # full Authorization header
current_merchant: ContextVar[str | None] = ContextVar("current_merchant", default=None)  # merchant of that token
