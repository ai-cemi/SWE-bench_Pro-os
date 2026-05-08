#!/usr/bin/env python3
"""Prepare a JSONL dataset of Python SWE-bench Pro tasks with a setup_script field.

For each Python task, adds a ``setup_script`` field containing the ENV, UV VENV,
and PYTHON SETUP bash sections derived from the instance's Dockerfiles.

Also adds an ``eval_scripts`` field: a list with one self-contained bash script
that writes run_script.sh and parser.py inline, then runs the evaluation tests.

Usage:
    python dataset_preprocessing/prepare_dataset.py --output artifacts/python_dataset.jsonl
    python dataset_preprocessing/prepare_dataset.py --output artifacts/python_dataset.jsonl --no-date-pin
"""

import argparse
import json
import sys
from pathlib import Path

from datasets import load_dataset

from dataset_preprocessing.dockerfile_to_bash import (
    DEFAULT_REPO_ROOT,
    VENV_PATH,
    build_sections,
    resolve_repo_root,
    is_python_dockerfile,
    load_local_dockerfiles,
)

_REPO_ROOT = Path(__file__).parent.parent


def build_eval_script(iid: str, row: dict) -> str:
    run_script_path = _REPO_ROOT / "run_scripts" / iid / "run_script.sh"
    parser_path = _REPO_ROOT / "run_scripts" / iid / "parser.py"

    run_script_content = run_script_path.read_text()
    parser_content = parser_path.read_text()

    test_files_csv = ",".join(json.loads(row["selected_test_files_to_run"]))

    return f"""\
#!/bin/bash
# set -e intentionally omitted: eval must survive test failures

EVAL_DIR="${{SWE_EVAL_DIR:-$(pwd)/agent-evaluation}}"
mkdir -p "$EVAL_DIR"

cat > "$EVAL_DIR/run_script.sh" << 'EOFRUNSCRIPT'
{run_script_content}
EOFRUNSCRIPT

cat > "$EVAL_DIR/parser.py" << 'EOFPARSER'
{parser_content}
EOFPARSER

bash "$EVAL_DIR/run_script.sh" "{test_files_csv}" > "$EVAL_DIR/stdout.log" 2> "$EVAL_DIR/stderr.log"
PYTEST_EXIT=$?
python "$EVAL_DIR/parser.py" "$EVAL_DIR/stdout.log" "$EVAL_DIR/stderr.log" "$EVAL_DIR/output.json"
exit $PYTEST_EXIT 
"""


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--output", required=True, help="Output JSONL path.")
    p.add_argument(
        "--no-date-pin",
        action="store_true",
        help="Strip pypi-timemachine without adding UV_EXCLUDE_NEWER.",
    )
    p.add_argument(
        "--venv-path",
        default=VENV_PATH,
        help=f"Path to the venv created by the setup script (default: {VENV_PATH!r}, relative to CWD at eval time).",
    )
    p.add_argument(
        "--repo-root",
        default=None,
        help=f"Repo root path baked into the setup script (default: {DEFAULT_REPO_ROOT!r}). "
        "At runtime the script also respects $SWE_REPO_ROOT if set.",
    )
    args = p.parse_args()
    args.repo_root = resolve_repo_root(args.repo_root)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    written = skipped = 0
    with out.open("w") as f:
        for row in load_dataset("ScaleAI/SWE-bench_Pro", split="test"):
            if row["repo_language"] != "python":
                continue
            iid = row["instance_id"]
            try:
                base, inst = load_local_dockerfiles(iid)
            except FileNotFoundError:
                print(f"[skip] {iid}: Dockerfiles not found", file=sys.stderr)
                skipped += 1
                continue
            if not is_python_dockerfile(base):
                skipped += 1
                continue
            try:
                eval_script = build_eval_script(iid, row)
            except FileNotFoundError:
                print(f"[skip] {iid}: run_scripts not found", file=sys.stderr)
                skipped += 1
                continue
            sections = build_sections(
                iid,
                base,
                inst,
                no_date_pin=args.no_date_pin,
                venv_path=args.venv_path,
                repo_root=args.repo_root,
            )
            record = dict(row)
            record["setup_script"] = sections.to_bash(
                skip_apt=True, skip_repo_setup=True
            )
            record["eval_scripts"] = [eval_script]
            f.write(json.dumps(record) + "\n")
            written += 1

    print(f"Wrote {written} records ({skipped} skipped) → {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
