#!/usr/bin/env python3
"""Probe ONE instance from the 'no test results parsed' bucket: inject a diagnostic
dump into eval.sh so we can see what pytest actually printed."""

import json
from pathlib import Path

from run_dataset import publish_and_wait, TASK_FIELDS

SCRIPT_DIR = Path(__file__).parent
TASK_JSON = SCRIPT_DIR / "task.json"
DATASET = SCRIPT_DIR.parent / "python_dataset_ubuntu24.jsonl"
# Pick one with non-empty p2p so we can see whether pytest even ran the tests.
PROBE_IID = "instance_ansible__ansible-e9e6001263f51103e96e58ad382660df0f3d0e39-v30a923fb5c164d6cd18280c02422f75e611e8fb2"
TIMEOUT = 900

DIAG = r"""
echo "=== PROBE: stdout (head 200) ==="
head -200 .swebench_stdout.log 2>&1 || true
echo "=== PROBE: stdout (tail 200) ==="
tail -200 .swebench_stdout.log 2>&1 || true
echo "=== PROBE: stderr (full) ==="
cat .swebench_stderr.log 2>&1 || true
echo "=== PROBE: output.json (full) ==="
cat output.json 2>&1 || true
echo "=== END PROBE ==="
"""


def patch_eval(script: str) -> str:
    marker = 'echo "eval.sh: run_script.sh exit code = $TEST_RC"'
    return script.replace(marker, marker + "\n" + DIAG, 1)


def main():
    config = json.loads(TASK_JSON.read_text())
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
        "run_id": config["run_id"],
        "task_id": PROBE_IID,
        "task": task,
        "inference_endpoint": config["inference_endpoint"],
        "harness": config["harness"],
    }
    result = publish_and_wait(payload, TIMEOUT)
    out = SCRIPT_DIR / "results.probe_no_results.jsonl"
    with out.open("w") as f:
        f.write(json.dumps({"instance_id": PROBE_IID, "raw": result}) + "\n")
    print(f"Wrote {out}")
    if result is None:
        print("ERROR: timeout or publish failed")
        return
    for r in (result.get("evaluation") or {}).get("eval_script_results", []):
        print(r.get("output", ""))


if __name__ == "__main__":
    main()
