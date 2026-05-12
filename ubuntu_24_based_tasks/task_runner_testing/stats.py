#!/usr/bin/env python3
"""Print resolved/unresolved stats and group unresolved by reason from results.all.jsonl."""

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

RESULTS = Path(__file__).parent / "results.all.jsonl"


def classify(row):
    if row.get("error"):
        return "harness_no_response"
    raw = row.get("raw") or {}
    err = raw.get("error") or ""
    if err:
        if "Connection reset by peer" in err or "Could not resolve host" in err or "Failed to connect" in err:
            return "network_git_fetch_failed"
        if "git fetch failed" in err:
            return "git_fetch_other"
        if "setup script failed" in err.lower():
            return "setup_script_failed"
        return "harness_error_other"
    ev = raw.get("evaluation") or {}
    results = ev.get("eval_script_results") or []
    if not results:
        return "no_eval_results"
    for r in results:
        out = r.get("output", "")
        m_run = re.search(r"run_script\.sh exit code = (\d+)", out)
        m_scorer = re.search(r"scorer exit code = (\d+)", out)
        run_exit = int(m_run.group(1)) if m_run else None
        scorer_exit = int(m_scorer.group(1)) if m_scorer else None
        if run_exit and run_exit != 0:
            return "run_script_failed"
        if scorer_exit is not None and scorer_exit != 0:
            return "tests_failed_scorer"
    return "unresolved_unclassified"


def repo_of(iid):
    return iid.split("__")[0].replace("instance_", "")


def main():
    total = resolved = unresolved = errors = 0
    reasons = Counter()
    by_repo = defaultdict(lambda: {"resolved": 0, "unresolved": 0})
    by_repo_reason = defaultdict(Counter)
    examples = defaultdict(list)

    with RESULTS.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            total += 1
            iid = row["instance_id"]
            repo = repo_of(iid)
            if row.get("error"):
                errors += 1
            if row.get("resolved"):
                resolved += 1
                by_repo[repo]["resolved"] += 1
            else:
                unresolved += 1
                by_repo[repo]["unresolved"] += 1
                r = classify(row)
                reasons[r] += 1
                by_repo_reason[repo][r] += 1
                if len(examples[r]) < 3:
                    raw = row.get("raw") or {}
                    hint = (raw.get("error") or "").strip().splitlines()[:1]
                    if not hint:
                        results = (raw.get("evaluation") or {}).get("eval_script_results") or []
                        if results:
                            hint = results[0].get("output", "").strip().splitlines()[:3]
                    examples[r].append((iid, hint))

    pct = lambda n: f"{n/total*100:.1f}%" if total else "-"
    print(f"Total:      {total}")
    print(f"Resolved:   {resolved} ({pct(resolved)})")
    print(f"Unresolved: {unresolved} ({pct(unresolved)})")
    print(f"Errors:     {errors}")
    print()
    print("=== Unresolved reasons ===")
    for reason, count in reasons.most_common():
        print(f"  {count:4d}  {reason}")
    print()
    print("=== By repo ===")
    for repo, counts in sorted(by_repo.items()):
        tot = counts["resolved"] + counts["unresolved"]
        print(f"  {repo:30s}  resolved={counts['resolved']:3d}/{tot:3d}  unresolved={counts['unresolved']:3d}")
    print()
    print("=== Unresolved by repo × reason ===")
    for repo, c in sorted(by_repo_reason.items()):
        print(f"\n  {repo}:")
        for reason, count in c.most_common():
            print(f"    {count:4d}  {reason}")
    print()
    print("=== Examples per reason ===")
    for reason, items in examples.items():
        print(f"\n[{reason}] ({reasons[reason]} total)")
        for iid, hint in items:
            print(f"  - {iid}")
            for h in hint:
                print(f"      {h[:140]}")


if __name__ == "__main__":
    main()
