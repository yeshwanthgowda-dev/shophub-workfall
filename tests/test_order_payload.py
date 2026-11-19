from tools.subagent_tools.order_payload import build_order_payload

RAW = {
    "id": 1042, "merchant": "A", "customer_name": "Maria Lopez", "customer_email": "maria@example.com",
    "customer_phone": "+1 415-555-0134", "status": "PAID", "total": 59.9, "placed_at": "2026-11-01T10:00:00Z",
    "customer_note": "Please call +1 415-555-0134 or mail maria@example.com",
}


def test_projection_drops_pii_and_note_text():
    view = build_order_payload(RAW).model_dump()
    assert set(view) == {"id", "status", "total", "placed_at", "has_note"}
    blob = str(view)
    for secret in ("Maria", "maria@example.com", "415", "Please call"):
        assert secret not in blob
    assert view["has_note"] is True
    assert build_order_payload({**RAW, "customer_note": ""}).has_note is False
    assert build_order_payload({**RAW, "customer_note": "   "}).has_note is False
