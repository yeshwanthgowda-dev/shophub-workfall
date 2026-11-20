"""Small command-line client: send one message, print the events as they arrive, and approve or reject a proposal.

    uv run python scripts/chat_client.py --token tok_demo_merchant_a "refund order 1002"
    uv run python scripts/chat_client.py --token tok_demo_merchant_a --thread <id> "status of order 1003"

The token is sent only in the Authorization header. Set ORCHESTRATOR_URL to change the server (default http://localhost:8000).
"""
import argparse
import json
import os
import sys

import httpx


def events(response: httpx.Response):
    """Yield (event name, data) from a server-sent-events response. Heartbeat comment lines are skipped."""
    name = None
    for line in response.iter_lines():
        if line.startswith("event: "):
            name = line.removeprefix("event: ")
        elif line.startswith("data: ") and name:
            yield name, json.loads(line.removeprefix("data: "))
            name = None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("message")
    parser.add_argument("--token", required=True)
    parser.add_argument("--thread")
    parser.add_argument("--url", default=os.environ.get("ORCHESTRATOR_URL", "http://localhost:8000"))
    args = parser.parse_args()
    headers = {"Authorization": f"Bearer {args.token}"}
    thread_id, proposal = args.thread, None

    with httpx.Client(base_url=args.url, timeout=None) as client:
        with client.stream("POST", "/chat", json={"message": args.message, **({"thread_id": args.thread} if args.thread else {})},
                           headers=headers) as response:
            if response.status_code != 200:
                response.read()
                print(f"HTTP {response.status_code}: {response.text}")
                return 1
            for name, data in events(response):
                if name == "token":
                    print(data["text"], end="", flush=True)
                    continue
                print(f"\n[{name}] {json.dumps(data)}")
                if name == "proposal":
                    proposal = data
                if name == "done":
                    thread_id = data["thread_id"]

        if proposal:
            answer = input(f"Approve {proposal['action']} of order {proposal['order_id']} (${proposal['amount']})? [y/N] ")
            verb = "approve" if answer.strip().lower() == "y" else "reject"
            resp = client.post(f"/threads/{thread_id}/proposals/{proposal['proposal_id']}/{verb}", headers=headers)
            print(f"[{verb}] HTTP {resp.status_code} {resp.text}")
    print(f"\nthread: {thread_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
