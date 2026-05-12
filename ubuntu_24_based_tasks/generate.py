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
# Source of truth: fresh dump from `ScaleAI/SWE-bench_Pro` HF dataset (Python subset).
# `artifacts/python_dataset.jsonl` came from a dead-end pipeline that silently
# filtered ~31 instances; do not use that file.
DATASET_PATH = REPO_ROOT / "ubuntu_24_based_tasks" / "raw" / "raw_python_dataset.jsonl"
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
# Fallback: ansible has a few ubuntu:20.04 base Dockerfiles that apt-install
# `python3.9`, `python3.9-dev`, etc. Match the highest version installed.
PY_APT_VERSION_RE = re.compile(r"\bpython3\.(\d+)\b")
HEREDOC_RE = re.compile(
    r"RUN\s+cat\s+<<\s*'?(EOFBUILD|EOFPREP)'?\s*>\s*\S+\s*\n(.*?)\n\1",
    re.DOTALL,
)
ENV_RE = re.compile(r'^ENV\s+([A-Z_][A-Z0-9_]*)\s*=?\s*(.+?)\s*$', re.MULTILINE)
TIMEMACHINE_DATE_RE = re.compile(r"pypi-timemachine\s+(\d{4}-\d{2}-\d{2})")


def parse_python_version(base_dockerfile: str) -> str:
    """Extract Python version from the base Dockerfile.

    Strategy:
      1. Match `FROM python:X.Y-…` (with optional ECR mirror prefix). Covers
         qutebrowser, openlibrary, and most ansible instances.
      2. Fallback for `FROM ubuntu:20.04`-style bases that apt-install
         `python3.X` packages — pick the highest X seen anywhere in the file.
      3. Final fallback for `FROM ubuntu:22.04`-style bases that apt-install
         only the unversioned `python3` package — match the python version
         that the upstream ubuntu image actually ships:
           ubuntu:20.04 -> 3.8
           ubuntu:22.04 -> 3.10
           ubuntu:24.04 -> 3.12
    """
    m = PY_VERSION_RE.search(base_dockerfile)
    if m:
        return m.group(1)
    apt_versions = [int(x) for x in PY_APT_VERSION_RE.findall(base_dockerfile)]
    if apt_versions:
        return f"3.{max(apt_versions)}"
    ubuntu_default = {
        "20.04": "3.8",
        "22.04": "3.10",
        "24.04": "3.12",
    }
    for ver, py in ubuntu_default.items():
        # Match `FROM …ubuntu:X.Y` (allows ECR / dockerhub mirror prefixes).
        if re.search(rf"FROM\s+\S*ubuntu:{re.escape(ver)}\b", base_dockerfile):
            return py
    raise ValueError("could not find a Python version in base Dockerfile")


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


def setuptools_cap(date_pin: str | None) -> str:
    """
    Pick a setuptools version spec based on date_pin.

    Three constraints fight here:
      - Old projects often break on modern setuptools (>=60, >=68, >=80) which
        progressively removed easy_install / setup.py-develop / etc.
      - uv's editable install (PEP 660 `build_editable`) was added in setuptools
        64. Anything older silently breaks `uv pip install -e .` even with
        `--no-build-isolation`, because uv calls build_editable directly.
      - setuptools 69+ ships `_distutils/_modified.py` that does
        `from jaraco.functools import splat`. When project requirements pin
        `jaraco.functools<4` (qutebrowser does at multiple base_commits), any
        `import distutils.*` in the venv (hunter, _distutils_hack precedence)
        crashes with `ImportError: cannot import name 'splat'`. Capping
        setuptools at <69 dodges this regardless of date_pin.

    Compromise: floor every cap at >=64 (so PEP 660 works) and use
    --no-build-isolation only for old date_pins (modern ones use build
    isolation so the build env has a coherent setuptools+jaraco set).
    """
    if date_pin and date_pin < "2022-06-01":
        # Old projects: setuptools 64-66 — first versions with PEP 660 / build_editable,
        # before setuptools started deprecating legacy fields (>=68 drops easy_install).
        return ">=64,<67"
    # Modern (or no date_pin): cap below 69 to avoid the splat ImportError when
    # project requirements drag in an old jaraco.functools.
    return ">=64,<69"


def needs_no_build_isolation(date_pin: str | None) -> bool:
    """
    Pre-2024 instances need --no-build-isolation for editable installs.

    Why: with UV_EXCLUDE_NEWER set, uv's build-isolation environment also
    respects the cutoff and pulls in OLD setuptools for the build (e.g.
    setuptools 62 at date_pin=2022-06-07), which can lack `build_editable`
    or break the project's setup.py. By disabling build isolation, the
    editable install uses the venv's already-installed setuptools (which we
    pinned to >=64 with --exclude-newer-package=setuptools=2024-01-01).

    For modern date_pins (>=2024-01-01) we KEEP build isolation: the venv
    holds whatever the project pinned in its requirements (e.g. old
    jaraco.functools), and forcing a modern venv setuptools to drive
    PEP 660 against those old pins breaks (e.g. setuptools 80 imports
    `jaraco.functools.splat` which doesn't exist in jaraco.functools<4).
    With build isolation re-enabled, uv builds against a coherent
    setuptools+jaraco set inside the isolated env.
    """
    return date_pin is not None and date_pin < "2024-01-01"


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
    re.compile(r'^pip3? install setuptools\b'),  # we manage setuptools ourselves (see cap)
    re.compile(r'^python3? -m pip install --upgrade pip\b'),  # same as above
    re.compile(r'^pip3? install --upgrade pip'),  # uv has no pip-upgrade concept
    re.compile(r'^apt-get\b'),  # all apt is in the base image
    re.compile(r'^apt\s+'),
    re.compile(r'^#'),
    re.compile(r'^python -c "import qutebrowser'),  # smoke checks at install time
    re.compile(r'^QT_QPA_PLATFORM=offscreen python -c'),
    # NOTE: keep `pip install pytest-rerunfailures` (will be translated to uv).
    # PYTEST_ADDOPTS exports --reruns=3 in env_vars from the upstream ENV
    # directive, so the plugin must actually be installed or pytest aborts.
]


# Rewrite `/app` (the upstream Dockerfile WORKDIR) to the runtime CWD.
# Use single quotes to defer shell expansion of $(pwd) to install.sh runtime.
APP_PATH_REWRITES = [
    (re.compile(r'(?<![A-Za-z0-9_])/app/'), '"$(pwd)/"'),    # "/app/<X>" → "$(pwd)/<X>"
    (re.compile(r'(?<![A-Za-z0-9_])/app(?![/A-Za-z0-9_-])'), '"$(pwd)"'),  # bare /app → "$(pwd)"
]

# Env-self-references like $PYTHONPATH break under `set -u` when the var is
# unset on entry to install.sh. Rewrite to a defensive default.
ENV_DEFENSIVE_REWRITES = [
    (re.compile(r'\$PYTHONPATH\b'), '${PYTHONPATH:-}'),
    (re.compile(r'\$LD_LIBRARY_PATH\b'), '${LD_LIBRARY_PATH:-}'),
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
      - rewrite hard-coded `/app` paths (upstream Dockerfile WORKDIR) to the
        runtime CWD via `"$(pwd)"`. Ansible's build heredoc sets `PYTHONPATH`,
        `PATH`, and `mkdir -p /app/...` — all break in our CWD-based contract.
      - rewrite self-referencing env-var expansions like `$PYTHONPATH` (which
        is unset under `set -u`) to `${PYTHONPATH:-}`.
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
        # Translate `pip install ...`, `pip3 install ...`, and
        # `python[3] -m pip install ...` -> `uv pip install ...`. The runner
        # image has no system pip/pip3 (uv-managed venv only); these all need
        # to route through uv. The `pip3` form is used by qutebrowser's
        # ubuntu:18.04-derived Dockerfiles.
        if re.match(r"^\s*(python3?\s+-m\s+)?pip3?\s+install\b", line):
            line = re.sub(
                r"^\s*(python3?\s+-m\s+)?pip3?\s+install\b",
                "uv pip install",
                line,
            )
            # Inject --no-build-isolation for `uv pip install -e .` on old projects.
            if no_build_isolation and re.search(r"\buv pip install\b.*\s-e\s+\.(\s|$)", line) \
               and "--no-build-isolation" not in line:
                line = line.replace(
                    "uv pip install", "uv pip install --no-build-isolation", 1,
                )
        # Rewrite /app paths and unsafe env-self-references.
        for rx, repl in APP_PATH_REWRITES:
            line = rx.sub(repl, line)
        for rx, repl in ENV_DEFENSIVE_REWRITES:
            line = rx.sub(repl, line)
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
# --seed: install pip+setuptools+wheel into the venv. Required by ansible-test
# (it shells out to `python -m pip install` from inside the venv during eval).
# Step 5 below overwrites the seeded setuptools with the version pinned by
# setuptools_cap when date_pin demands an old one.
uv venv --seed --python @@PYTHON_VERSION@@ .venv

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

@@XVFB_START@@
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

@@XVFB_STOP@@

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
    if date_pin:
        # UV_EXCLUDE_NEWER would filter out the setuptools versions we need
        # (PEP 660 / build_editable wants >=64, released 2022-09; we want <69
        # to dodge the jaraco.functools.splat ImportError).
        # --exclude-newer-package=setuptools=YYYY lifts the cutoff just for setuptools.
        setuptools_line = (
            f'uv pip install --exclude-newer-package=setuptools=2024-01-01 '
            f'--exclude-newer-package=wheel=2024-01-01 '
            f'"setuptools{cap}" wheel  # date_pin={date_pin}'
        )
    else:
        setuptools_line = f'uv pip install "setuptools{cap}" wheel'
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


XVFB_START_BLOCK = """\
# Headless display (some repos' tests need a running X server + dbus, e.g. qutebrowser).
export DISPLAY=:99
Xvfb :99 -screen 0 1024x768x24 >/dev/null 2>&1 &
XVFB_PID=$!
sleep 1
"""

XVFB_STOP_BLOCK = """\
# Stop Xvfb (best-effort).
kill "$XVFB_PID" 2>/dev/null || true
"""


def needs_xvfb(run_script: str) -> bool:
    """Decide whether eval.sh should launch Xvfb based on the run_script body.

    Heuristic: the run_script references Xvfb (in case it expects one to be
    pre-running), or sets QT_QPA_PLATFORM, or exports DISPLAY=. None of those
    appear in ansible's or openlibrary's run_scripts; qutebrowser hits them all.
    """
    return bool(re.search(r"\b(Xvfb|QT_QPA_PLATFORM|DISPLAY=)", run_script))


def rewrite_app_paths(text: str) -> str:
    """Apply /app -> $(pwd) and $PYTHONPATH -> ${PYTHONPATH:-} rewrites."""
    for rx, repl in APP_PATH_REWRITES:
        text = rx.sub(repl, text)
    for rx, repl in ENV_DEFENSIVE_REWRITES:
        text = rx.sub(repl, text)
    return text


# Instances that test the warning machinery itself (e.g.
# `TestRegex::test_passed_warnings` uses `pytest.warns(...)` to assert that
# specific DeprecationWarnings ARE raised). Overriding `filterwarnings=`
# globally lets those warnings be filtered out by something upstream, so
# `pytest.warns` doesn't see them and the test fails. Hand-curated list —
# add an instance here only after confirming it both:
#   (a) does NOT need the override to bypass `filterwarnings = error` at
#       conftest-import time (i.e. its conftest already imports cleanly), and
#   (b) DOES regress when the override is injected.
#
# Future improvement: auto-detect this case by reading the test files listed
# in `selected_test_files_to_run` at base_commit and grepping for
# `pytest.warns(`. We didn't go that route yet because:
#   - it requires a local checkout of the upstream repo at base_commit
#     (extra git work at generation time), and
#   - we currently have exactly 2 known cases. Revisit if more
#     pytest.warns-style regressions show up after milestone 3.
FILTERWARNINGS_OVERRIDE_OPT_OUT: set[str] = {
    "instance_qutebrowser__qutebrowser-996487c43e4fcc265b541f9eca1e7930e3c5cf05-v2ef375ac784985212b1805e1d0431dc8f1b3c171",
    "instance_qutebrowser__qutebrowser-52708364b5f91e198defb022d1a5b4b3ebd9b563-v2ef375ac784985212b1805e1d0431dc8f1b3c171",
}


# Match a pytest invocation at the START of a command (the start of a line,
# possibly after `dbus-run-session --` or env-var assignments like
# `QT_QPA_PLATFORM=offscreen`). Anchored to line start to avoid matching
# `pytest` inside echo strings or comments.
PYTEST_COMMAND_RE = re.compile(
    r"""
    ^                                       # start of line
    (?P<indent>\s*)                         # leading whitespace
    (?P<prefix>                             # optional env-var assignments / dbus-run-session
        (?:[A-Z][A-Z0-9_]*=\S+\s+)*         # env-var assignments: FOO=bar BAZ=qux
        (?:dbus-run-session\s+--\s+)?       # dbus-run-session wrapper
        (?:[A-Z][A-Z0-9_]*=\S+\s+)*         # more env-vars (can also come AFTER dbus-run-session)
    )
    (?P<cmd>
        (?:python[0-9.]*(?:\s+-[A-Za-z]+)?\s+-m\s+)?  # optional `python[-flags] -m`
        pytest
    )
    (?=\s|$)                                # end of pytest token: space or EOL
    """,
    re.VERBOSE,
)


def inject_filterwarnings_override(run_script: str) -> str:
    """Inject `--override-ini=filterwarnings=` into every pytest invocation.

    Why: qutebrowser's `pytest.ini` sets `filterwarnings = error`, which turns
    third-party `DeprecationWarning`s (notably `pypeg2==2.15.2`'s `\\s` regex,
    triggered by Python 3.9.20+'s stricter `ast.parse`) into fatal
    conftest-import failures (`pytest exits 4`). Suppressing the ini filter
    lets test collection proceed; the parser still sees per-test PASS/FAIL
    lines and the scorer decides resolved/unresolved from those.

    Only matches pytest invocations at the START of a command line (so
    `echo "pytest would be preferred"` is left alone). Idempotent: no-op if
    `filterwarnings=` is already present on the line.
    """
    out_lines = []
    for line in run_script.splitlines():
        if "filterwarnings=" in line:
            out_lines.append(line)
            continue
        new_line = PYTEST_COMMAND_RE.sub(
            r'\g<indent>\g<prefix>\g<cmd> --override-ini="filterwarnings="',
            line,
        )
        out_lines.append(new_line)
    return "\n".join(out_lines)


def render_eval(
    *,
    instance_id: str,
    run_script: str,
    parser_py: str,
    selected_tests: list[str],
) -> str:
    # Pass tests to run_script.sh as one comma-separated arg (the script handles both forms).
    tests_arg = ",".join(selected_tests)
    xvfb = needs_xvfb(run_script)
    # Rewrite /app and self-ref env vars in the inlined run_script too
    # (ansible run_scripts reference /app/bin/ansible-test and PYTHONPATH=/app).
    run_script = rewrite_app_paths(run_script)
    # Override pytest.ini's `filterwarnings = error` so unrelated third-party
    # DeprecationWarnings don't kill conftest import (qutebrowser/pypeg2).
    # Opt-out instances test the warning machinery itself (pytest.warns) and
    # regress under the override.
    if instance_id not in FILTERWARNINGS_OVERRIDE_OPT_OUT:
        run_script = inject_filterwarnings_override(run_script)
    return _fill(
        EVAL_TMPL,
        instance_id=instance_id,
        run_script=run_script.rstrip("\n"),
        parser_py=parser_py.rstrip("\n"),
        selected_tests_arg=f'"{tests_arg}"',
        xvfb_start=XVFB_START_BLOCK.rstrip("\n") if xvfb else "",
        xvfb_stop=XVFB_STOP_BLOCK.rstrip("\n") if xvfb else "",
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

    # Preserve existing JSONL records for OTHER instances so successive
    # `--repo X` / `--repo Y` invocations accumulate rather than overwrite.
    # Records for any instance_id we're regenerating now will be replaced.
    new_ids = {r["instance_id"] for r in records}
    preserved: list[dict] = []
    if OUT_JSONL.exists():
        with open(OUT_JSONL) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    prev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if prev.get("instance_id") not in new_ids:
                    preserved.append(prev)

    ok = 0
    failed: list[tuple[str, str]] = []
    with open(OUT_JSONL, "w") as out_fh:
        for prev in preserved:
            out_fh.write(json.dumps(prev) + "\n")
        for r in records:
            try:
                gen = generate_one(r)
                emit(r, gen, out_fh)
                ok += 1
            except Exception as e:
                failed.append((r["instance_id"], f"{type(e).__name__}: {e}"))

    total_lines = len(preserved) + ok
    print(f"generated {ok}/{len(records)} install.sh + eval.sh pairs "
          f"(JSONL now has {total_lines} records)", file=sys.stderr)
    print(f"  per-instance dirs: {OUT_DIR}", file=sys.stderr)
    print(f"  extended JSONL:    {OUT_JSONL}", file=sys.stderr)
    if failed:
        print(f"  {len(failed)} failures:", file=sys.stderr)
        for iid, err in failed[:10]:
            print(f"    - {iid}: {err}", file=sys.stderr)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
