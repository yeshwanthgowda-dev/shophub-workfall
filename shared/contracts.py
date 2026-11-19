"""Contracts shared by the orchestrator and the orders agent (docs/EXECUTION.md section 2).

Imports from no service. Models for proposals and write outcomes are added in the
modules that use them.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

OrderStatus = Literal["PLACED", "PAID", "SHIPPED", "DELIVERED", "CANCELLED", "REFUNDED"]


class OrderView(BaseModel):
    """The only order shape the model may see. Unknown fields (email, phone, name) are rejected."""
    model_config = ConfigDict(extra="forbid")

    id: int
    status: OrderStatus
    total: float
    placed_at: str
    has_note: bool


# ---------- model-facing tools: inputs ----------

class ListOrdersArgs(BaseModel):
    status: OrderStatus | None = None
    older_than_days: int | None = Field(default=None, ge=0)


class GetOrderArgs(BaseModel):
    order_id: int


class FindOrdersByCustomerNameArgs(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class GetOrderNoteArgs(BaseModel):
    order_id: int


class ProposeArgs(BaseModel):
    """propose_refund and propose_cancel. No amount, merchant, email or phone."""
    order_id: int


# Names the model may call. Anything else, including refund_order and cancel_order, is rejected.
READ_TOOLS = ("list_orders", "get_order", "find_orders_by_customer_name", "get_order_note")
PROPOSE_TOOLS = ("propose_refund", "propose_cancel")
MODEL_TOOLS = READ_TOOLS + PROPOSE_TOOLS

# ---------- A2A: one JSON-RPC `message/send`, one skill per request ----------

READ_SKILLS = READ_TOOLS
WRITE_SKILLS = ("refund_order", "cancel_order")  # approve path only, never bound to the model


class A2ASkillCall(BaseModel):
    """Data part of the message. The token is NOT here: it travels in the Authorization header only."""
    skill: str
    args: dict[str, Any] = Field(default_factory=dict)
    merchant_claim: str | None = None  # a claim to verify against /auth/me, never an identity


# ---------- Gate A ----------

class VerifierVerdict(BaseModel):
    matches: bool
