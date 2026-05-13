#!/usr/bin/env python3
"""Merge results.retry.jsonl into results.all.jsonl, replacing rows by instance_id."""

import json
from pathlib import Path

BASE = Path(__file__).parent
ALL_PATH = BASE / "results.all.jsonl"
RETRY_PATH = BASE / "results.retry.jsonl"


def main():
    retry_by_id = {}
    with RETRY_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            retry_by_id[row["instance_id"]] = row
    print(f"Loaded {len(retry_by_id)} retry rows")

    replaced = 0
    out_lines = []
    with ALL_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            iid = row["instance_id"]
            if iid in retry_by_id:
                out_lines.append(json.dumps(retry_by_id[iid]))
                replaced += 1
            else:
                out_lines.append(line)

    print(f"Replaced {replaced} rows in {ALL_PATH.name}")

    seen_ids = {json.loads(l)["instance_id"] for l in out_lines}
    missing = set(retry_by_id) - seen_ids
    if missing:
        print(f"WARNING: retry ids not found in {ALL_PATH.name}: {missing}")

    with ALL_PATH.open("w") as f:
        f.write("\n".join(out_lines) + "\n")
    print(f"Wrote {len(out_lines)} lines to {ALL_PATH}")


if __name__ == "__main__":
    main()
