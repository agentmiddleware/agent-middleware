"""Verify every receipt in an AMW state dir and print a compact summary."""

import json
import sys
from pathlib import Path

from goose_amw.receipts import verify

state = Path(sys.argv[1])
pub = (state / "receipt_public_key.b64").read_text().strip()
bad = 0
for line in (state / "receipts.jsonl").read_text().splitlines():
    r = json.loads(line)
    ok = verify(r, pub)
    bad += not ok
    b = r["body"]
    print(
        "OK " if ok else "BAD",
        b["issued_at"],
        b["tool"],
        b["decision"],
        b["outcome"],
        b["args_hash"][:19],
        b.get("enforced_by", "proxy"),
        b.get("agent_tool_call_request_id") or "-",
        b.get("protocol", "-"),
    )
sys.exit(1 if bad else 0)
