# Project
ShopHub support assistant: a multi-tenant LangGraph orchestrator plus an A2A orders sub-agent and a mock store API. This is the take-home from `docs/shophub.pdf` (requirements R0–R11).

Read these before changing anything:
- `docs/shophub.pdf`: the spec.
- `docs/EXECUTION.md`: the build runbook. Module order, contracts, tests, done-gates, status table, cut order.
- `docs/concepts_brief.md`: the numbered security invariants and the reasons behind each decision.
- `docs/DESIGN.md`: living draft of the final DESIGN.md.

# How to work
- One module at a time, in the order in `docs/EXECUTION.md`: design, code, test, walkthrough.
- Do not start the next module until the module's done-gate command passes and the author has confirmed they understand it. After each module, explain it in plain terms and wait.
- Update the status table in `docs/EXECUTION.md` after each module.
- If a module runs more than 50% over its estimate, stop and propose its simpler variant. Do not cut anything from the "do not cut" list.
- When a decision in the docs seems wrong, say so and ask. Do not silently deviate.
- Commit after each of design notes, code and tests for a module, with a message saying what and why. Do not commit unless asked.

# Hard rules
- No secrets in the repo. Never commit `.env`, API keys, tokens (even revoked), or `*.db`, `*.db-wal`, `*.db-shm`. Seed tokens are public fake fixtures only.
- The bearer token travels only in the HTTP `Authorization` header. It must never enter graph state, tool arguments, checkpoints, logs, SSE events, A2A message bodies, or any database file.
- The model never executes a refund or cancel. Only the approve path does, using the approver's token.
- Customer email, phone and name never reach the model or the checkpoint.
- Tests must pass with no API key set (`LLM_PROVIDER=fake`).
- Python 3.11+, dependencies pinned in a lockfile, `LANGCHAIN_TRACING_V2=false`.

# Stack
Python, LangGraph, FastAPI, `a2a-sdk` (pinned; hand-rolled fallback if it costs more than 90 minutes), SQLite checkpointer (`AsyncSqliteSaver`), pytest, respx, Docker Compose. One LLM factory function; fake LLM by default; OpenRouter optional.

# Layout (create in module 0)
`store/`, `orders_agent/`, `orchestrator/`, `shared/` (allowlist, scrubber, untrusted wrapper; imports from no service), `evals/`, `tests/`, `scripts/`, `docs/`.
At the end, `README.md` and `DESIGN.md` must sit at the repo root.
