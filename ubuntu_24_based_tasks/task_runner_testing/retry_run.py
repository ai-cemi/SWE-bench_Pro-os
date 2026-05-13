#!/usr/bin/env python3
"""Retry runner: re-runs entries from a JSONL dataset via NATS, writes per-row results."""

import argparse
import json
from pathlib import Path

from run_dataset import publish_and_wait, TASK_FIELDS

SCRIPT_DIR = Path(__file__).parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=str(SCRIPT_DIR / "retry_dataset.jsonl"))
    parser.add_argument("--results", default=str(SCRIPT_DIR / "results.retry.jsonl"))
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument(
        "--task-json",
        default=str(SCRIPT_DIR / "task.json"),
        help="Path to task.json; supplies run_id, inference_endpoint, harness.",
    )
    parser.add_argument(
        "--nats-port",
        type=int,
        default=4222,
        help="Host port for NATS. 4222 = temp_dev stack, 4333 = temp_dev_m3 stack.",
    )
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    results_path = Path(args.results)
    task_json_path = Path(args.task_json)

    config = json.loads(task_json_path.read_text())
    run_id = config["run_id"]
    inference_endpoint = config["inference_endpoint"]
    harness = config["harness"]

    entries = []
    with dataset_path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))

    print(f"Running {len(entries)} entries from {dataset_path} -> {results_path}")

    with results_path.open("a") as out_fh:
        for i, entry in enumerate(entries, start=1):
            instance_id = entry["instance_id"]
            task = {field: entry[field] for field in TASK_FIELDS if field in entry}
            payload = {
                "run_id": run_id,
                "task_id": instance_id,
                "task": task,
                "inference_endpoint": inference_endpoint,
                "harness": harness,
            }
            print(f"[{i}/{len(entries)}] {instance_id} ...", end=" ", flush=True)
            result = publish_and_wait(payload, args.timeout, nats_port=args.nats_port)
            if result is None:
                print("ERROR (timeout or publish failed)")
                row = {"instance_id": instance_id, "resolved": None, "error": True, "raw": None}
            else:
                resolved = result.get("evaluation", {}).get("resolved", False)
                print("resolved" if resolved else "unresolved")
                row = {"instance_id": instance_id, "resolved": resolved, "error": False, "raw": result}
            out_fh.write(json.dumps(row) + "\n")
            out_fh.flush()


if __name__ == "__main__":
    main()
