"""Order projection. The only way an order leaves the orders agent.

Copies named fields one by one. Never `**raw`, so a new store column cannot leak.
Drops email, phone, customer name and note text. Adds `has_note` (invariants 4, 5, 10).
"""
from __future__ import annotations

from typing import Any, Mapping

from shared.contracts import OrderView


def has_note(raw: Mapping[str, Any]) -> bool:
    note = raw.get("customer_note")
    return isinstance(note, str) and note.strip() != ""


def build_order_payload(raw: Mapping[str, Any]) -> OrderView:
    return OrderView(
        id=raw["id"],
        status=raw["status"],
        total=raw["total"],
        placed_at=raw["placed_at"],
        has_note=has_note(raw),
    )
