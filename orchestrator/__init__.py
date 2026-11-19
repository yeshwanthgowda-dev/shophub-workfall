import os

# Hard rule: tracing off, so no state or token can be shipped to a tracing backend.
os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
