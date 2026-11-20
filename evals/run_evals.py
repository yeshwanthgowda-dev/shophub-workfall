"""Mini evaluation harness: scripted conversations with expected outcomes, one PASS/FAIL row per case.

    python -m evals.run_evals                 # fake LLM, no key needed (the default)
    python -m evals.run_evals --case approve_executes
    LLM_PROVIDER=openrouter OPENROUTER_API_KEY=... LLM_MODEL=... python -m evals.run_evals   # a real model

There is no blended score on purpose: each case stands alone. The exit code is 1 if any case failed.
With a real model, cases marked "fake_only" (the ones that script a misbehaving model) are skipped.
"""
import argparse
import asyncio
import json
import os
import pathlib
import sys
import tempfile

from evals.system import TOKEN_A, TOKEN_B, Scripted, build_system
from orchestrator.fake_llm import FakeChatModel
from store.seed import ORDERS

SCENARIOS = pathlib.Path(__file__).with_name("scenarios.json")
TOKENS = {"a": TOKEN_A, "b": TOKEN_B}


def load_scenarios() -> list[dict]:
    return json.loads(SCENARIOS.read_text())


def make_model(spec, fake: bool):
    """The model for one case. None means the default (the fake model, or the real one from the factory)."""
    if not fake:
        from orchestrator.llm import make_llm
        return make_llm(max_tokens=1024)
    if spec == "gullible":
        return FakeChatModel(gullible=True)
    if spec == "gullible_propose":
        return FakeChatModel(gullible=True, gullible_tool="propose_refund")
    if isinstance(spec, dict):
        return Scripted(script=[[(name, args) for name, args in step] for step in spec["script"]])
    return FakeChatModel()


async def run_case(case: dict, directory, fake: bool = True) -> list[str]:
    """Run one scenario in a fresh system. Returns the list of failed expectations (empty means pass)."""
    system = await build_system(pathlib.Path(directory), llm=make_model(case.get("llm"), fake))
    token = TOKENS[case.get("token", "a")]
    expect, problems = case["expect"], []
    try:
        thread_id, result = None, None
        for message in case["turns"]:
            result = await system.chat(message, token, thread_id=thread_id)
            thread_id = result.json()["thread_id"] or thread_id
        assert result.status_code == 200, f"HTTP {result.status_code}"
        reply = result.json()["reply"] or ""
        started = [data["name"] for name, data in result.events if name == "tool_started"]
        proposal = result.json().get("proposal")

        for name in expect.get("tool_called", []):
            if name not in started:
                problems.append(f"tool {name} was not called (called: {started})")
        if expect.get("no_tools") and started:
            problems.append(f"no tool should run, but {started} did")
        for text in expect.get("reply_contains", []):
            if text not in reply:
                problems.append(f"reply lacks {text!r}")
        for text in expect.get("reply_not_contains", []):
            if text in reply:
                problems.append(f"reply contains {text!r}")
        if expect.get("no_proposal") and proposal:
            problems.append("a proposal was raised")
        if "proposal" in expect:
            if not proposal:
                problems.append("no proposal was raised")
            else:
                problems += [f"proposal {key} is {proposal.get(key)!r}, wanted {want!r}"
                             for key, want in expect["proposal"].items() if proposal.get(key) != want]
        if expect.get("no_seed_pii"):
            secrets = {v for o in ORDERS for v in (o["customer_email"], o["customer_phone"], o["customer_name"])}
            problems += [f"the stream contains {secret!r}" for secret in secrets if secret in result.text]
        if "then" in expect and proposal:
            resp = await system.decide(expect["then"]["decide"], thread_id, proposal["proposal_id"], token)
            if resp.status_code != 200 or resp.json()["status"] != expect["then"]["status"]:
                problems.append(f"{expect['then']['decide']} gave HTTP {resp.status_code} {resp.text}")
        for order_id, wanted in expect.get("order_status", {}).items():
            actual = await system.order_status(int(order_id), token)
            if actual != wanted:
                problems.append(f"order {order_id} is {actual}, wanted {wanted}")
    except Exception as exc:  # a crash is a failed case, not a crashed run
        problems.append(f"crashed: {type(exc).__name__}: {exc}")
    finally:
        await system.close()
    return problems


async def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", help="run only this case id")
    args = parser.parse_args(argv)
    fake = os.environ.get("LLM_PROVIDER", "fake").lower() == "fake"
    cases = [c for c in load_scenarios() if not args.case or c["id"] == args.case]
    if not cases:
        print(f"no such case: {args.case}")
        return 2
    passed = failed = skipped = 0
    for case in cases:
        if case.get("fake_only") and not fake:
            skipped += 1
            print(f"SKIP  {case['id']}  (scripts a misbehaving model: fake LLM only)")
            continue
        with tempfile.TemporaryDirectory() as directory:
            problems = await run_case(case, directory, fake)
        if problems:
            failed += 1
            print(f"FAIL  {case['id']}")
            for problem in problems:
                print(f"        - {problem}")
        else:
            passed += 1
            print(f"PASS  {case['id']}")
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped  ({'fake LLM' if fake else 'real model'})")
    return 1 if failed else 0


if __name__ == "__main__":
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
    sys.exit(asyncio.run(main()))
