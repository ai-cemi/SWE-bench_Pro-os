#!/usr/bin/env python3
"""Probe whether injecting `--override-ini=filterwarnings=` into the pytest
invocation unblocks the run_exit_4 bucket (qutebrowser, pypeg2 \\s DeprecationWarning)."""

import json
import re
from pathlib import Path

from run_dataset import publish_and_wait, TASK_FIELDS

SCRIPT_DIR = Path(__file__).parent
TASK_JSON = SCRIPT_DIR / "task.json"
DATASET = SCRIPT_DIR.parent / "python_dataset_ubuntu24.jsonl"
PROBE_IID = "instance_qutebrowser__qutebrowser-01d1d1494411380d97cac14614a829d3a69cecaf-v2ef375ac784985212b1805e1d0431dc8f1b3c171"
TIMEOUT = 900

# Insert `--override-ini=filterwarnings=` after `python -m pytest -v`/`pytest -v` invocations
# inside the run_script that gets inlined in eval.sh.
PYTEST_RE = re.compile(r"(\b(?:python[0-9.]*\s+-m\s+)?pytest)(\s+-v\b|\s+--)", re.MULTILINE)


def patch_eval(script: str) -> str:
    def repl(m: re.Match) -> str:
        return f'{m.group(1)} --override-ini="filterwarnings="{m.group(2)}'
    new_script, n = PYTEST_RE.subn(repl, script)
    print(f"  injected --override-ini=filterwarnings= into {n} pytest invocation(s)")
    return new_script


def main():
    config = json.loads(TASK_JSON.read_text())
    run_id = config["run_id"]
    inference_endpoint = config["inference_endpoint"]
    harness = config["harness"]

    entry = None
    with DATASET.open() as f:
        for line in f:
            row = json.loads(line)
            if row.get("instance_id") == PROBE_IID:
                entry = row
                break
    if entry is None:
        raise SystemExit(f"could not find {PROBE_IID}")

    entry["eval_scripts"] = [patch_eval(s) for s in entry["eval_scripts"]]

    task = {field: entry[field] for field in TASK_FIELDS if field in entry}
    payload = {
        "run_id": run_id,
        "task_id": PROBE_IID,
        "task": task,
        "inference_endpoint": inference_endpoint,
        "harness": harness,
    }
    result = publish_and_wait(payload, TIMEOUT)
    out = SCRIPT_DIR / "results.probe_run4_fix.jsonl"
    with out.open("w") as f:
        f.write(json.dumps({"instance_id": PROBE_IID, "raw": result}) + "\n")
    print(f"Wrote {out}")
    if result is None:
        print("ERROR: timeout or publish failed")
        return
    print(f"resolved={result.get('evaluation', {}).get('resolved')}")
    for r in (result.get("evaluation") or {}).get("eval_script_results", []):
        print("=== eval script output ===")
        print(r.get("output", "")[:2000])


if __name__ == "__main__":
    main()
