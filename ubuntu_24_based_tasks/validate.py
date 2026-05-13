#!/usr/bin/env python3
"""
Validate generated install/eval scripts against the swebench-pro-ubuntu24 base image.

Drives entirely from ubuntu_24_based_tasks/python_dataset_ubuntu24.jsonl — each record carries
everything needed: `setup_script`, `eval_scripts[0]`, `patch`, `test_patch`,
`base_commit`, `repo`, `env_vars`, `python_version`, `date_pin`.

For each selected instance, mimics the itf-demo task-runner flow inside a
disposable container:

  Pre-patch run (expect scorer exit 1):
    1. git clone @ base_commit
    2. bash setup_script        (install.sh)
    3. git apply test_patch     (runner's evaluator does this between harness and eval)
    4. bash eval_scripts[0]     (eval.sh)            → expect EXIT 1

  Post-patch run (expect scorer exit 0):
    1. git clone @ base_commit
    2. bash setup_script
    3. git apply <gold patch>   (harness output, here = dataset.patch)
    4. git apply test_patch
    5. bash eval_scripts[0]                          → expect EXIT 0

Usage:
  python ubuntu_24_based_tasks/validate.py --instance-id <iid>
  python ubuntu_24_based_tasks/validate.py --repo qutebrowser                # all instances of one repo
  python ubuntu_24_based_tasks/validate.py --repo qutebrowser --index 0      # the first qutebrowser instance
  python ubuntu_24_based_tasks/validate.py --repo qutebrowser --index 0,5,42 # specific indices
  python ubuntu_24_based_tasks/validate.py --repo qutebrowser --sample 3     # 3 random instances
  python ubuntu_24_based_tasks/validate.py --diverse                         # one per (python_version, date_pin) bucket
"""

from __future__ import annotations

import argparse
import json
import random
import shlex
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from textwrap import dedent

REPO_ROOT = Path(__file__).resolve().parent.parent
JSONL_PATH = REPO_ROOT / "ubuntu_24_based_tasks" / "python_dataset_ubuntu24.jsonl"
BASE_IMAGE = "swebench-pro-ubuntu24"

REPO_URLS = {
    "qutebrowser/qutebrowser": "https://github.com/qutebrowser/qutebrowser.git",
    "ansible/ansible": "https://github.com/ansible/ansible.git",
    "internetarchive/openlibrary": "https://github.com/internetarchive/openlibrary.git",
}


def load_records() -> list[dict]:
    with open(JSONL_PATH) as f:
        return [json.loads(line) for line in f]


def pick_instances(records: list[dict], args) -> list[dict]:
    if args.instance_id:
        return [r for r in records if r["instance_id"] == args.instance_id]
    if args.repo:
        records = [r for r in records if r["repo"].endswith(f"/{args.repo}")]
    if args.index:
        # Comma-separated list of indices into the (possibly repo-filtered) list.
        idxs = [int(s) for s in args.index.split(",") if s.strip()]
        return [records[i] for i in idxs if 0 <= i < len(records)]
    if args.diverse:
        bucket: dict[tuple, dict] = {}
        for r in records:
            key = (r.get("python_version"), r.get("date_pin"))
            bucket.setdefault(key, r)
        return list(bucket.values())
    if args.sample:
        random.seed(0)
        return random.sample(records, min(args.sample, len(records)))
    return records


def run_in_container(
    *,
    repo_url: str,
    base_commit: str,
    setup_script: str,
    eval_script: str,
    test_patch: str,
    harness_patch: str | None,
    timeout: int,
    label: str,
) -> tuple[int, str]:
    """
    Run one pre- or post-patch evaluation in a disposable container.

    Streams docker stdout+stderr to OUR stderr in real time so the user can
    follow pytest progress live. Also accumulates the output and returns the
    last 4 KB so failures are diagnosable.
    """
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        (td_path / "setup.sh").write_text(setup_script)
        (td_path / "eval.sh").write_text(eval_script)
        (td_path / "test_patch.diff").write_text(test_patch)
        if harness_patch:
            (td_path / "harness_patch.diff").write_text(harness_patch)

        harness_step = ""
        if harness_patch:
            harness_step = (
                'echo "::: applying harness (gold) patch :::"\n'
                "git apply --whitespace=fix /inputs/harness_patch.diff || exit 90\n"
            )

        # Distinct exit codes per failed step so the validator can tell setup
        # failures apart from eval failures. Reserve <90 for eval (pytest exit
        # codes pass through), 90+ for our own steps.
        runner = dedent(f"""
            mkdir -p /tmp/jobs/x/workspace && cd /tmp/jobs/x/workspace
            # Retry the clone: github.com occasionally drops connections.
            for attempt in 1 2 3 4; do
              if git clone --quiet {shlex.quote(repo_url)} . ; then break ; fi
              echo "::: clone attempt $attempt failed; retrying in 5s :::" >&2
              rm -rf ./* ./.[!.]* 2>/dev/null
              sleep 5
              if [ $attempt -eq 4 ]; then exit 92 ; fi
            done
            git checkout --quiet {shlex.quote(base_commit)} || exit 93

            echo "::: running setup_script :::"
            if ! bash /inputs/setup.sh; then
              echo "::: setup_script FAILED :::" >&2
              exit 94
            fi

            {harness_step}
            echo "::: applying test_patch :::"
            if [ -s /inputs/test_patch.diff ]; then
              git apply --whitespace=fix /inputs/test_patch.diff || exit 91
            fi

            echo "::: running eval_script :::"
            bash /inputs/eval.sh
            EVAL_RC=$?
            echo "::: eval_script exit code = $EVAL_RC :::"
            exit $EVAL_RC
        """).strip()

        proc = subprocess.Popen(
            [
                "docker", "run", "--rm",
                "-v", f"{td_path}:/inputs:ro",
                BASE_IMAGE,
                "bash", "-c", runner,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge into one stream so order is preserved
            text=True,
            bufsize=1,                 # line-buffered
        )
        prefix = f"    [{label}] "
        captured: list[str] = []
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                captured.append(line)
                # Live-stream to stderr, prefixed so the user can tell which run.
                print(prefix + line, file=sys.stderr, flush=True)
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            captured.append(f"<timeout after {timeout}s>")
        tail = "\n".join(captured)[-4096:]
        return proc.returncode if proc.returncode is not None else -1, tail


def validate_one(rec: dict, *, run_post_patch: bool, timeout: int) -> dict:
    iid = rec["instance_id"]
    repo_url = REPO_URLS.get(rec["repo"]) or f"https://github.com/{rec['repo']}.git"
    base_commit = rec["base_commit"]
    setup = rec["setup_script"]
    eval_sh = rec["eval_scripts"][0]
    test_patch = rec.get("test_patch", "")
    gold_patch = rec["patch"]

    result = {"instance_id": iid, "python_version": rec.get("python_version"),
              "date_pin": rec.get("date_pin")}

    # Exit codes >=90 are our own step-fail signals; anything <90 is from eval.
    STEP_FAIL = {90: "harness_patch", 91: "test_patch", 92: "clone",
                 93: "checkout", 94: "setup_script"}

    # Pre-patch (no harness): expect EXIT 1 (the 4 expected-failing tests fail).
    pre_rc, pre_tail = run_in_container(
        repo_url=repo_url, base_commit=base_commit,
        setup_script=setup, eval_script=eval_sh,
        test_patch=test_patch, harness_patch=None,
        timeout=timeout, label="pre",
    )
    result["pre_patch_rc"] = pre_rc
    if pre_rc in STEP_FAIL:
        result["pre_patch_ok"] = False
        result["pre_patch_failed_step"] = STEP_FAIL[pre_rc]
        result["pre_patch_tail"] = pre_tail
    else:
        result["pre_patch_ok"] = (pre_rc == 1)
        if not result["pre_patch_ok"]:
            result["pre_patch_tail"] = pre_tail

    # Post-patch (gold harness): expect EXIT 0.
    if run_post_patch:
        post_rc, post_tail = run_in_container(
            repo_url=repo_url, base_commit=base_commit,
            setup_script=setup, eval_script=eval_sh,
            test_patch=test_patch, harness_patch=gold_patch,
            timeout=timeout, label="post",
        )
        result["post_patch_rc"] = post_rc
        if post_rc in STEP_FAIL:
            result["post_patch_ok"] = False
            result["post_patch_failed_step"] = STEP_FAIL[post_rc]
            result["post_patch_tail"] = post_tail
        else:
            result["post_patch_ok"] = (post_rc == 0)
            if not result["post_patch_ok"]:
                result["post_patch_tail"] = post_tail

    result["ok"] = result["pre_patch_ok"] and (
        not run_post_patch or result["post_patch_ok"]
    )
    return result


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--instance-id", help="Pick exactly this instance_id")
    p.add_argument("--repo", help="Filter to one repo, e.g. qutebrowser")
    p.add_argument("--index", help="Comma-separated indices into the (filtered) list, e.g. 0,5,42")
    p.add_argument("--diverse", action="store_true",
                   help="One instance per (python_version, date_pin) bucket")
    p.add_argument("--sample", type=int, default=0,
                   help="Random sample N (works with --repo)")
    p.add_argument("--skip-post-patch", action="store_true",
                   help="Only run pre-patch (faster smoke check)")
    p.add_argument("--timeout", type=int, default=1800)
    args = p.parse_args()

    records = load_records()
    selected = pick_instances(records, args)
    if not selected:
        print("no instances matched", file=sys.stderr)
        return 2

    print(f"Validating {len(selected)} instance(s) against image '{BASE_IMAGE}'",
          file=sys.stderr)

    failures: list[dict] = []
    for i, rec in enumerate(selected, 1):
        iid = rec["instance_id"]
        print(f"\n[{i}/{len(selected)}] {iid}", file=sys.stderr)
        print(f"  python={rec.get('python_version')}  date_pin={rec.get('date_pin')}",
              file=sys.stderr)

        res = validate_one(rec, run_post_patch=not args.skip_post_patch,
                           timeout=args.timeout)

        flag = "✓" if res["ok"] else "✗"
        pre_extra = f" ({res['pre_patch_failed_step']} failed)" if res.get("pre_patch_failed_step") else ""
        post_str = ""
        if "post_patch_rc" in res:
            post_extra = f" ({res['post_patch_failed_step']} failed)" if res.get("post_patch_failed_step") else ""
            post_str = f"  post_rc={res['post_patch_rc']}{post_extra}"
        print(f"  {flag} pre_rc={res['pre_patch_rc']}{pre_extra}{post_str}", file=sys.stderr)

        if not res["ok"]:
            failures.append(res)
            print("  --- tail of failing run ---", file=sys.stderr)
            tail = res.get("post_patch_tail") or res.get("pre_patch_tail", "")
            for line in tail.splitlines()[-15:]:
                print(f"    {line}", file=sys.stderr)

    print(f"\n=== {len(selected) - len(failures)}/{len(selected)} OK ===", file=sys.stderr)
    if failures:
        print("Failing instances:", file=sys.stderr)
        for f in failures:
            print(f"  - {f['instance_id']} "
                  f"(pre={f['pre_patch_rc']} post={f.get('post_patch_rc', 'n/a')})",
                  file=sys.stderr)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
