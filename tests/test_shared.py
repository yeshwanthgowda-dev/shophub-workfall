import pathlib
import re

import pytest
from pydantic import ValidationError

from shared.contracts import MODEL_TOOLS, WRITE_SKILLS, OrderView

def test_contract_models_forbid_extra_fields():
    with pytest.raises(ValidationError):
        OrderView(id=1, status="PAID", total=1, placed_at="x", has_note=False, customer_email="a@b.co")


def test_write_skills_never_model_tools():
    assert not set(WRITE_SKILLS) & set(MODEL_TOOLS)


def test_shared_imports_no_service():
    for f in pathlib.Path("shared").rglob("*.py"):
        src = f.read_text()
        assert not re.search(r"^\s*(?:from|import)\s+(?:store|orders_agent|orchestrator)\b", src, re.M), f
