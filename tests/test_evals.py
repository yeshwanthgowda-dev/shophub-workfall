import pytest

from evals.run_evals import load_scenarios, run_case


def test_at_least_the_five_required_kinds_of_case_exist():
    ids = {case["id"] for case in load_scenarios()}
    assert len(ids) >= 5 and len(ids) == len(load_scenarios())  # unique ids
    for needed in ("get_order_status", "refund_proposal_raised", "injection_refund_order_ignored", "cross_merchant_refused_a"):
        assert needed in ids


@pytest.mark.parametrize("case", load_scenarios(), ids=lambda case: case["id"])
async def test_scenario(case, tmp_path):
    assert await run_case(case, tmp_path) == []
