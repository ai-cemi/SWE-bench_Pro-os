#!/usr/bin/env python3
"""Batch task runner: publishes each entry from python_dataset-2.jsonl to NATS and tracks results."""

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
TASK_JSON = SCRIPT_DIR / "task.json"
DATASET = SCRIPT_DIR / "openlibrary.jsonl"
RESULTS_FILE = SCRIPT_DIR / "results.jsonl"
NATS_IMAGE = "natsio/nats-box:latest"

TASK_FIELDS = [
    "instance_id", "repo", "base_commit", "problem_statement",
    "setup_script", "test_patch", "patch", "eval_scripts",
]


def nats_url(host: str) -> str:
    return f"nats://{host}:4222"


def docker_net_args(host: str) -> list[str]:
    if platform.system() == "Darwin":
        return []
    return ["--network", "host"]


def nats_host() -> str:
    return "host.docker.internal" if platform.system() == "Darwin" else "localhost"


def publish_and_wait(payload: dict, timeout: int) -> dict | None:
    host = nats_host()
    url = nats_url(host)
    net = docker_net_args(host)
    run_id = payload["run_id"]
    task_subject = f"tasks.run.{run_id}"
    result_subject = f"results.run.{run_id}"
    payload_str = json.dumps(payload)
    
    nats_auth_args = ["--user", "natsuser", "--password", "natspassword"]

    sub_cmd = [
        "docker", "run", "--rm", *net, NATS_IMAGE,
        "nats", "sub",
        *nats_auth_args,
        "--server", url,
        "--count", "1",
        "--timeout", f"{timeout}s",
        "--raw",
        result_subject,
    ]

    sub_proc = subprocess.Popen(sub_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    time.sleep(1)

    # Use stdin (`--force-stdin` + `-i`) instead of passing the payload as argv.
    # Some payloads (e.g. ansible-92) exceed ARG_MAX on Linux and fail with
    # OSError: [Errno 7] Argument list too long.
    pub_cmd = [
        "docker", "run", "--rm", "-i", *net, NATS_IMAGE,
        "nats", "publish",
        *nats_auth_args,
        "--server", url,
        "--force-stdin",
        task_subject,
    ]
    pub_result = subprocess.run(pub_cmd, input=payload_str, capture_output=True, text=True)
    if pub_result.returncode != 0:
        sub_proc.kill()
        return None

    try:
        stdout, _ = sub_proc.communicate(timeout=timeout + 5)
        if sub_proc.returncode != 0:
            return None
        return json.loads(stdout.decode())
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        sub_proc.kill()
        return None


def main():
    parser = argparse.ArgumentParser(description="Run all dataset tasks via NATS")
    parser.add_argument("--timeout", type=int, default=600, help="Seconds to wait per task")
    parser.add_argument("--start-from", type=int, default=0, dest="start_from",
                        help="Skip the first N entries (0-based)")
    parser.add_argument("--limit", type=int, default=None, help="Only run N tasks")
    parser.add_argument("--dataset", default=str(DATASET),
                        help="Path to dataset JSONL (default: %(default)s)")
    parser.add_argument("--results", default=str(RESULTS_FILE),
                        help="Path to results JSONL (appended; default: %(default)s)")
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    results_path = Path(args.results)

    config = json.loads(TASK_JSON.read_text())
    run_id = config["run_id"]
    inference_endpoint = config["inference_endpoint"]
    harness = config["harness"]

    entries = []
    with dataset_path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))

    total = len(entries)
    subset = entries[args.start_from:]
    if args.limit is not None:
        subset = subset[: args.limit]

    print(f"Dataset: {total} entries total, running {len(subset)} "
          f"(start_from={args.start_from}, limit={args.limit})")
    print(f"Run ID: {run_id}")
    print(f"Dataset: {dataset_path}")
    print(f"Results: {results_path}")
    print()

    resolved_ids = []
    unresolved_ids = []
    error_ids = []

    results_fh = results_path.open("a")

    try:
        for i, entry in enumerate(subset, start=1):
            instance_id = entry.get("instance_id", f"entry_{args.start_from + i - 1}")
            task = {field: entry[field] for field in TASK_FIELDS if field in entry}
            payload = {
                "run_id": run_id,
                "task_id": instance_id,
                "task": task,
                "inference_endpoint": inference_endpoint,
                "harness": harness,
            }

            print(f"[{i}/{len(subset)}] {instance_id} ...", end=" ", flush=True)
            result = publish_and_wait(payload, args.timeout)

            if result is None:
                print("ERROR (timeout or publish failed)")
                error_ids.append(instance_id)
                row = {"instance_id": instance_id, "resolved": None, "error": True, "raw": None}
            else:
                resolved = result.get("evaluation", {}).get("resolved", False)
                status = "resolved" if resolved else "unresolved"
                print(status)
                if resolved:
                    resolved_ids.append(instance_id)
                else:
                    unresolved_ids.append(instance_id)
                row = {"instance_id": instance_id, "resolved": resolved, "error": False, "raw": result}

            results_fh.write(json.dumps(row) + "\n")
            results_fh.flush()
    finally:
        results_fh.close()

    print()
    print("=== Summary ===")
    print(f"Total run:   {len(subset)}")
    print(f"Resolved:    {len(resolved_ids)}")
    print(f"Unresolved:  {len(unresolved_ids)}")
    print(f"Errors:      {len(error_ids)}")

    if resolved_ids:
        print(f"\nResolved ({len(resolved_ids)}):")
        for iid in resolved_ids:
            print(f"  + {iid}")

    if error_ids:
        print(f"\nErrors ({len(error_ids)}):")
        for iid in error_ids:
            print(f"  ! {iid}")


if __name__ == "__main__":
    main()
