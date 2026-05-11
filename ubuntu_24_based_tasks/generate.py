#!/usr/bin/env python3
"""
Generate per-instance install.sh + eval.sh + an extended JSONL for the
SWE-bench Pro Python tasks, targeting a plain ubuntu:24.04 + uv runtime.

Inputs (raw, NOT the artifacts produced by dataset_preprocessing/):
  - artifacts/python_dataset.jsonl  (Python subset of the HF dataset)
  - dockerfiles/base_dockerfile/<iid>/Dockerfile     (FROM line -> python_version)
  - dockerfiles/instance_dockerfile/<iid>/Dockerfile (EOFBUILD body -> install steps)
  - run_scripts/<iid>/{run_script.sh, parser.py}     (inlined into eval.sh)

Outputs:
  - ubuntu_24_based_tasks/out/<iid>/install.sh
  - ubuntu_24_based_tasks/out/<iid>/eval.sh
  - ubuntu_24_based_tasks/python_dataset_ubuntu24.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_PATH = REPO_ROOT / "artifacts" / "python_dataset.jsonl"
BASE_DF_DIR = REPO_ROOT / "dockerfiles" / "base_dockerfile"
INST_DF_DIR = REPO_ROOT / "dockerfiles" / "instance_dockerfile"
RUN_SCRIPTS_DIR = REPO_ROOT / "run_scripts"
OUT_DIR = REPO_ROOT / "ubuntu_24_based_tasks" / "out"
OUT_JSONL = REPO_ROOT / "ubuntu_24_based_tasks" / "python_dataset_ubuntu24.jsonl"

REPO_URLS = {
    "qutebrowser/qutebrowser": "https://github.com/qutebrowser/qutebrowser.git",
    "ansible/ansible": "https://github.com/ansible/ansible.git",
    "internetarchive/openlibrary": "https://github.com/internetarchive/openlibrary.git",
}


# ---------------------------------------------------------------------------
# Dockerfile parsing
# ---------------------------------------------------------------------------

PY_VERSION_RE = re.compile(r"python:(\d+\.\d+)")
HEREDOC_RE = re.compile(
    r"RUN\s+cat\s+<<\s*'?(EOFBUILD|EOFPREP)'?\s*>\s*\S+\s*\n(.*?)\n\1",
    re.DOTALL,
)
ENV_RE = re.compile(r'^ENV\s+([A-Z_][A-Z0-9_]*)\s*=?\s*(.+?)\s*$', re.MULTILINE)
TIMEMACHINE_DATE_RE = re.compile(r"pypi-timemachine\s+(\d{4}-\d{2}-\d{2})")


def parse_python_version(base_dockerfile: str) -> str:
    """Extract Python version from `FROM python:X.Y-slim` (with optional ECR mirror prefix)."""
    m = PY_VERSION_RE.search(base_dockerfile)
    if not m:
        raise ValueError("could not find python:X.Y in base Dockerfile")
    return m.group(1)


def parse_env_vars(*dockerfiles: str) -> dict[str, str]:
    """Merge ENV directives from base + instance Dockerfiles."""
    env = {}
    for df in dockerfiles:
        for k, v in ENV_RE.findall(df):
            v = v.strip()
            # ENV K="value" -> strip outer quotes if balanced
            if len(v) >= 2 and v[0] == v[-1] and v[0] in {'"', "'"}:
                v = v[1:-1]
            env[k] = v
    return env


def extract_heredoc(instance_dockerfile: str, tag: str) -> str:
    """Return the body of a `RUN cat <<'TAG' > /file.sh\n...\nTAG` block."""
    for found_tag, body in HEREDOC_RE.findall(instance_dockerfile):
        if found_tag == tag:
            return body.strip("\n")
    return ""


def extract_date_pin(build_body: str) -> str | None:
    m = TIMEMACHINE_DATE_RE.search(build_body)
    return m.group(1) if m else None


def setuptools_cap(date_pin: str | None) -> str | None:
    """
    Pick a setuptools version spec based on date_pin.

    Two constraints fight here:
      - Old projects often break on modern setuptools (>=60, >=68, >=80) which
        progressively removed easy_install / setup.py-develop / etc.
      - uv's editable install (PEP 660 `build_editable`) was added in setuptools
        64. Anything older silently breaks `uv pip install -e .` even with
        `--no-build-isolation`, because uv calls build_editable directly.

    Compromise: floor every cap at >=64 (so PEP 660 works) and use
    --no-build-isolation to keep that pinned setuptools active during the build.
    """
    if not date_pin:
        return None
    if date_pin < "2022-06-01":
        # Old projects: setuptools 64-66 — first versions with PEP 660 / build_editable,
        # before setuptools started deprecating legacy fields (>=68 drops easy_install).
        return ">=64,<67"
    if date_pin < "2024-01-01":
        return ">=64,<68"
    return None


def needs_no_build_isolation(date_pin: str | None) -> bool:
    """Old projects need --no-build-isolation so our pinned setuptools is used."""
    return date_pin is not None and date_pin < "2022-06-01"


# ---------------------------------------------------------------------------
# Build-body translation: Dockerfile EOFBUILD -> uv-based install steps
# ---------------------------------------------------------------------------

# Lines we drop entirely (pypi-timemachine bootstrap, pip config, etc.)
DROP_LINE_RES = [
    re.compile(r"^pip install\s+pypi-timemachine"),
    re.compile(r"^pypi-timemachine\s+"),
    re.compile(r"^pip config "),
    re.compile(r"^sleep\s+\d+\s*$"),
    re.compile(r"^echo\s+\""),  # the BUILD START / END banners
    re.compile(r"^export PYTEST_ADDOPTS"),  # handled by .swebench_env
    re.compile(r"^export QT_"),
    re.compile(r"^export DISPLAY"),
    re.compile(r"^export QUTE_"),
    re.compile(r"^export QTWEB"),
    re.compile(r"^set -e\s*$"),  # install.sh sets its own pipeline behavior
    re.compile(r"^cd /app\s*$"),  # we already chdir'd via CWD contract
    re.compile(r'^pip install setuptools\b'),  # we manage setuptools ourselves (see cap)
    re.compile(r"^pip install --upgrade pip"),  # uv has no pip-upgrade concept
    re.compile(r'^#'),
    re.compile(r'^python -c "import qutebrowser'),  # smoke checks at install time
    re.compile(r'^QT_QPA_PLATFORM=offscreen python -c'),
    re.compile(r"^pip install pytest-rerunfailures"),  # we'll handle in install body if needed
]


def translate_build_body(build_body: str, *, no_build_isolation: bool) -> list[str]:
    """
    Translate the EOFBUILD heredoc into uv-based install steps.

    Strategy: keep the line structure of the Dockerfile build script, but:
      - drop lines matching DROP_LINE_RES (pypi-timemachine, env exports, etc.)
      - rewrite `pip install ...` -> `uv pip install ...`
      - for editable installs (`pip install -e .`) on old projects, add
        `--no-build-isolation` so the venv's pinned setuptools is used instead
        of uv spinning up an isolated build env with a modern setuptools that
        either lacks PEP 660 support (too old) or breaks old setup.py (too new).
      - skip qutebrowser's `scripts/link_pyqt.py`: it symlinks PyQt5 from system
        site-packages into the venv, which made sense when PyQt5 came from apt.
        In our uv-managed venv PyQt5 is already inside .venv, so the script is
        a no-op that crashes loudly (FileNotFoundError on .tox-info.json) and
        was tolerated upstream via `|| true`. Replace with a comment echo so
        the intent is preserved without the noise.
    """
    out: list[str] = []
    for raw in build_body.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("#!"):
            continue
        if any(rx.match(line) for rx in DROP_LINE_RES):
            continue
        # Replace `python scripts/link_pyqt.py ...` (irrelevant in uv-managed venv).
        if re.search(r"\bscripts/link_pyqt\.py\b", line):
            out.append('echo "skipped: scripts/link_pyqt.py (irrelevant in uv-managed venv)"')
            continue
        # Translate pip -> uv pip
        if re.match(r"^\s*pip\s+install\b", line):
            line = re.sub(r"^\s*pip\s+install\b", "uv pip install", line)
            # Inject --no-build-isolation for `uv pip install -e .` on old projects.
            if no_build_isolation and re.search(r"\buv pip install\b.*\s-e\s+\.(\s|$)", line) \
               and "--no-build-isolation" not in line:
                line = line.replace(
                    "uv pip install", "uv pip install --no-build-isolation", 1,
                )
        out.append(line)
    return out


# ---------------------------------------------------------------------------
# Script rendering
# ---------------------------------------------------------------------------

INSTALL_TMPL = r"""#!/usr/bin/env bash
# Generated by ubuntu_24_based_tasks/generate.py for @@INSTANCE_ID@@
# Targets: ubuntu:24.04 + uv (pre-installed). Runs after `git clone` + `git checkout <base_commit>`.

set -euo pipefail

cd "$(pwd)"

# --- 1. Reset repo to base_commit and apply before_repo_set_cmd ---------------
@@BEFORE_REPO_SET_CMD@@

# --- 2. Static env vars from base + instance Dockerfile ENV directives -------
@@ENV_EXPORTS@@

# --- 3. Venv activation (so anything install steps export propagates correctly)
export VIRTUAL_ENV="$(pwd)/.venv"
export PATH="$VIRTUAL_ENV/bin:$PATH"

# --- 4. Create venv with the Python version pinned by the base image --------
uv venv --python @@PYTHON_VERSION@@ .venv

# --- 5. Setuptools build-time pin (only for pre-2024 date_pin) --------------
@@SETUPTOOLS_PIN@@

# --- 6. Per-instance Python deps (translated from instance Dockerfile) ------
@@INSTALL_STEPS@@

# --- 7. Dump the resulting env so eval.sh can re-source it -------------------
# Snapshot every exported variable (static env from step 2 + anything the install
# steps exported, e.g. PYTEST_QT_API). A denylist skips shell-managed vars that
# the runner's shell already provides and that we should NOT overwrite.
#
# `export -p` gives correctly-quoted `declare -x KEY="VALUE"` lines that bash can
# safely re-source — much safer than parsing `env`, which leaves values unquoted
# and breaks on newlines / spaces / shell metacharacters.
python3 - .swebench_env <<'DUMP_EOF'
import os, sys, subprocess, re
out_path = sys.argv[1]
deny_exact = {
    "PWD","OLDPWD","HOME","USER","LOGNAME","SHELL","SHLVL","TERM","HOSTNAME",
    "HOSTTYPE","MACHTYPE","OSTYPE","PS1","PS2","PS3","PS4","IFS","LINES",
    "COLUMNS","LS_COLORS","MAIL","_","BASHOPTS","SHELLOPTS","DIRSTACK",
    "PIPESTATUS","RANDOM","SECONDS","GROUPS","EUID","UID","PPID","FUNCNAME",
    "LINENO","XDG_RUNTIME_DIR","XDG_SESSION_ID","XDG_SESSION_TYPE","DBUS_SESSION_BUS_ADDRESS",
}
deny_prefix = ("BASH_", "LC_", "LANG")
lines = ["# Generated by install.sh at $(date)"]
for k, v in sorted(os.environ.items()):
    if k in deny_exact: continue
    if any(k.startswith(p) for p in deny_prefix): continue
    if k == "LANG": continue
    # Single-quote and escape any embedded single quotes the safe way.
    escaped = v.replace("'", "'\"'\"'")
    lines.append(f"export {k}='{escaped}'")
with open(out_path, "w") as f:
    f.write("\n".join(lines) + "\n")
DUMP_EOF

echo "install.sh: complete (env dumped to .swebench_env)"
"""


EVAL_TMPL = r"""#!/usr/bin/env bash
# Generated by ubuntu_24_based_tasks/generate.py for @@INSTANCE_ID@@
# Runs AFTER `git apply patch.diff`. NO `set -e` — failures must not abort scoring.

# Re-source env vars persisted by install.sh (also activates the venv via PATH).
# shellcheck disable=SC1091
source .swebench_env

# Headless display (qutebrowser tests need a running X server + dbus).
export DISPLAY=:99
Xvfb :99 -screen 0 1024x768x24 >/dev/null 2>&1 &
XVFB_PID=$!
sleep 1

# Write run_script.sh and parser.py (inlined verbatim from run_scripts/<iid>/).
cat > .swebench_run_script.sh <<'RUN_SCRIPT_EOF'
@@RUN_SCRIPT@@
RUN_SCRIPT_EOF
chmod +x .swebench_run_script.sh

cat > .swebench_parser.py <<'PARSER_EOF'
@@PARSER_PY@@
PARSER_EOF

# Run the project's test entrypoint with the selected test files.
bash .swebench_run_script.sh @@SELECTED_TESTS_ARG@@ \
    > .swebench_stdout.log 2> .swebench_stderr.log
TEST_RC=$?
echo "eval.sh: run_script.sh exit code = $TEST_RC"

# Parse pytest output -> output.json.
python .swebench_parser.py .swebench_stdout.log .swebench_stderr.log output.json

# Stop Xvfb (best-effort).
kill "$XVFB_PID" 2>/dev/null || true

# --- Scoring: pass iff parsed output.json has no FAILED / ERROR entries -----
# Decision based on parser output, NOT $TEST_RC (which is polluted by
# pytest collection errors, --continue-on-collection-errors, Qt teardown crashes).
python - <<'SCORE_EOF'
import json, sys
from pathlib import Path

try:
    tests = json.loads(Path("output.json").read_text()).get("tests", [])
except (OSError, ValueError) as e:
    print(f"eval.sh: could not read output.json: {e}", file=sys.stderr)
    sys.exit(1)

if not tests:
    print("eval.sh: no test results parsed from output", file=sys.stderr)
    sys.exit(1)

bad = [t for t in tests if t.get("status") in ("FAILED", "ERROR")]
if bad:
    print(f"eval.sh: {len(bad)}/{len(tests)} tests failed", file=sys.stderr)
    for t in bad[:20]:
        print(f"  - {t['name']}: {t['status']}", file=sys.stderr)
    sys.exit(1)

print(f"eval.sh: all {len(tests)} tests passed")
sys.exit(0)
SCORE_EOF
SCORE_RC=$?
echo "eval.sh: scorer exit code = $SCORE_RC"
exit $SCORE_RC
"""


def _fill(template: str, **kw: str) -> str:
    out = template
    for k, v in kw.items():
        out = out.replace(f"@@{k.upper()}@@", v)
    return out


def render_install(
    *,
    instance_id: str,
    python_version: str,
    env_vars: dict[str, str],
    before_repo_set_cmd: str,
    date_pin: str | None,
    install_steps: list[str],
) -> str:
    cap = setuptools_cap(date_pin)
    if cap is not None:
        # For old projects, UV_EXCLUDE_NEWER would filter out the setuptools
        # version we need (PEP 660 / build_editable needs >=64, released 2022-09).
        # --exclude-newer-package=setuptools=YYYY lifts the cutoff just for setuptools.
        setuptools_line = (
            f'uv pip install --exclude-newer-package=setuptools=2024-01-01 '
            f'--exclude-newer-package=wheel=2024-01-01 '
            f'"setuptools{cap}" wheel  # date_pin={date_pin}'
        )
    else:
        setuptools_line = "uv pip install setuptools wheel"
    if needs_no_build_isolation(date_pin):
        setuptools_line += "\n# build isolation disabled for editable installs (see uv pip install -e . below)"

    env_exports = []
    if date_pin:
        env_exports.append(f'export UV_EXCLUDE_NEWER={date_pin}')
    for k, v in env_vars.items():
        # Re-quote values; if value already has surrounding quotes we stripped them
        # at parse time, so add them back.
        if any(c in v for c in [' ', '"', "'"]):
            v_quoted = '"' + v.replace('"', '\\"') + '"'
        else:
            v_quoted = v
        env_exports.append(f'export {k}={v_quoted}')

    return _fill(
        INSTALL_TMPL,
        instance_id=instance_id,
        before_repo_set_cmd=before_repo_set_cmd.strip(),
        env_exports="\n".join(env_exports),
        python_version=python_version,
        setuptools_pin=setuptools_line,
        install_steps="\n".join(install_steps),
    )


def render_eval(
    *,
    instance_id: str,
    run_script: str,
    parser_py: str,
    selected_tests: list[str],
) -> str:
    # Pass tests to run_script.sh as one comma-separated arg (the script handles both forms).
    tests_arg = ",".join(selected_tests)
    return _fill(
        EVAL_TMPL,
        instance_id=instance_id,
        run_script=run_script.rstrip("\n"),
        parser_py=parser_py.rstrip("\n"),
        selected_tests_arg=f'"{tests_arg}"',
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

@dataclass
class Generated:
    instance_id: str
    install_script: str
    eval_script: str
    env_vars: dict[str, str]
    date_pin: str | None
    python_version: str


def strip_test_checkout(before_repo_set_cmd: str) -> str:
    """
    Drop `git checkout <commit> -- <files>` lines from before_repo_set_cmd.

    Context: the itf-demo task runner applies `test_patch` separately, between
    harness and eval. If install.sh also pulls test files from the eval_commit,
    the runner's `git apply test_patch` will fail (files already at eval state).
    So we keep the repo at base_commit and let the runner do the test_patch.
    """
    kept = []
    for line in before_repo_set_cmd.splitlines():
        # Match `git checkout <SHA> -- <paths>`. Discard.
        if re.match(r"^\s*git\s+checkout\s+\S+\s+--\s+\S+", line):
            continue
        kept.append(line)
    return "\n".join(kept)


def generate_one(record: dict) -> Generated:
    iid = record["instance_id"]
    base_df = (BASE_DF_DIR / iid / "Dockerfile").read_text()
    inst_df = (INST_DF_DIR / iid / "Dockerfile").read_text()
    run_script = (RUN_SCRIPTS_DIR / iid / "run_script.sh").read_text()
    parser_py = (RUN_SCRIPTS_DIR / iid / "parser.py").read_text()

    python_version = parse_python_version(base_df)
    env_vars = parse_env_vars(base_df, inst_df)
    build_body = extract_heredoc(inst_df, "EOFBUILD")
    date_pin = extract_date_pin(build_body)
    install_steps = translate_build_body(
        build_body,
        no_build_isolation=needs_no_build_isolation(date_pin),
    )

    # Selected tests are stored as a JSON-encoded string in the dataset.
    sel = record.get("selected_test_files_to_run") or "[]"
    if isinstance(sel, str):
        sel = json.loads(sel)

    # before_repo_set_cmd is plain shell text (multiline); strip the test-file
    # checkout lines — the runner applies test_patch separately.
    before_cmd = strip_test_checkout(record.get("before_repo_set_cmd", "").strip())

    install_sh = render_install(
        instance_id=iid,
        python_version=python_version,
        env_vars=env_vars,
        before_repo_set_cmd=before_cmd,
        date_pin=date_pin,
        install_steps=install_steps,
    )
    eval_sh = render_eval(
        instance_id=iid,
        run_script=run_script,
        parser_py=parser_py,
        selected_tests=sel,
    )

    return Generated(
        instance_id=iid,
        install_script=install_sh,
        eval_script=eval_sh,
        env_vars=env_vars,
        date_pin=date_pin,
        python_version=python_version,
    )


def emit(record: dict, gen: Generated, out_jsonl_fh) -> None:
    out_dir = OUT_DIR / gen.instance_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "install.sh").write_text(gen.install_script)
    (out_dir / "eval.sh").write_text(gen.eval_script)
    (out_dir / "install.sh").chmod(0o755)
    (out_dir / "eval.sh").chmod(0o755)

    # Build the extended record: keep upstream fields, then overwrite ours.
    # Drop dead-end fields from the previous attempt to avoid confusion.
    # Use the itf-demo runner protocol field names: `setup_script` (String) and
    # `eval_scripts` (Vec<String>) — see workload_agentic_coding_protocol::TaskDescription.
    extended = {k: v for k, v in record.items() if k not in {"setup_script", "eval_scripts"}}
    extended["python_version"] = gen.python_version
    extended["setup_script"] = gen.install_script
    extended["eval_scripts"] = [gen.eval_script]
    extended["env_vars"] = gen.env_vars
    extended["date_pin"] = gen.date_pin
    out_jsonl_fh.write(json.dumps(extended) + "\n")


def load_records(repo_filter: str | None, instance_id_filter: str | None) -> list[dict]:
    records = []
    with open(DATASET_PATH) as f:
        for line in f:
            r = json.loads(line)
            if instance_id_filter and r["instance_id"] != instance_id_filter:
                continue
            if repo_filter and not r["repo"].endswith(f"/{repo_filter}"):
                continue
            records.append(r)
    return records


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--instance-id", help="Generate scripts for one instance")
    g.add_argument("--repo", help="Generate scripts for one repo (e.g. qutebrowser)")
    g.add_argument("--all-python", action="store_true", help="Generate all 235 Python instances")
    args = p.parse_args()

    if not (args.instance_id or args.repo or args.all_python):
        p.error("must pass one of --instance-id / --repo / --all-python")

    records = load_records(args.repo, args.instance_id)
    if not records:
        print("no matching records", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    ok = 0
    failed: list[tuple[str, str]] = []
    with open(OUT_JSONL, "w") as out_fh:
        for r in records:
            try:
                gen = generate_one(r)
                emit(r, gen, out_fh)
                ok += 1
            except Exception as e:
                failed.append((r["instance_id"], f"{type(e).__name__}: {e}"))

    print(f"generated {ok}/{len(records)} install.sh + eval.sh pairs", file=sys.stderr)
    print(f"  per-instance dirs: {OUT_DIR}", file=sys.stderr)
    print(f"  extended JSONL:    {OUT_JSONL}", file=sys.stderr)
    if failed:
        print(f"  {len(failed)} failures:", file=sys.stderr)
        for iid, err in failed[:10]:
            print(f"    - {iid}: {err}", file=sys.stderr)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
