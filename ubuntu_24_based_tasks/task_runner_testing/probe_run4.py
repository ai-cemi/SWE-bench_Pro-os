#!/usr/bin/env python3
"""Probe the run_exit_4 bucket: inject diagnostic dumps into eval.sh and re-publish.

We patch the eval_scripts of one representative instance so that on failure it
prints the captured stdout/stderr (and a few env diagnostics), so the harness
result includes the actual pytest error message.
"""

import json
from pathlib import Path

from run_dataset import publish_and_wait, TASK_FIELDS

SCRIPT_DIR = Path(__file__).parent
TASK_JSON = SCRIPT_DIR / "task.json"
DATASET = SCRIPT_DIR.parent / "python_dataset_ubuntu24.jsonl"
PROBE_IID = "instance_qutebrowser__qutebrowser-01d1d1494411380d97cac14614a829d3a69cecaf-v2ef375ac784985212b1805e1d0431dc8f1b3c171"
TIMEOUT = 900

DIAG_BLOCK = r"""
# === PROBE DIAGNOSTIC DUMP ===
echo "=== PROBE: pwd ==="
pwd
echo "=== PROBE: python + pytest ==="
which python; python --version
which pytest && pytest --version || echo "no pytest on PATH"
echo "=== PROBE: tests/ tree ==="
ls -la tests/unit/utils/test_version.py tests/unit/components/test_blockutils.py 2>&1 || true
echo "=== PROBE: stdout log (head 200) ==="
head -200 .swebench_stdout.log 2>&1 || true
echo "=== PROBE: stderr log (full) ==="
cat .swebench_stderr.log 2>&1 || true
echo "=== END PROBE ==="
"""


def patch_eval(script: str) -> str:
    # Inject diagnostic right after eval.sh prints run_script exit code.
    marker = 'echo "eval.sh: run_script.sh exit code = $TEST_RC"'
    return script.replace(marker, marker + "\n" + DIAG_BLOCK, 1)


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
    out = SCRIPT_DIR / "results.probe_run4.jsonl"
    with out.open("w") as f:
        f.write(json.dumps({"instance_id": PROBE_IID, "raw": result}) + "\n")
    print(f"Wrote {out}")
    if result is None:
        print("ERROR: timeout or publish failed")
        return
    for r in (result.get("evaluation") or {}).get("eval_script_results", []):
        print("=== eval script output ===")
        print(r.get("output", ""))


if __name__ == "__main__":
    main()
