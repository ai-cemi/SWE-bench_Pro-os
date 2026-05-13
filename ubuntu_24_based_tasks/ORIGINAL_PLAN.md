# Plan: Rebase Python tasks onto plain `ubuntu:24.04` — start fresh in `ubuntu_24_based_tasks/`

## Context

SWE-bench Pro ships per-instance Dockerfiles where each task uses a different base image (`python:3.9-slim`, `python:3.11-slim`, `python:3.12.2-slim-bookworm`, `ubuntu:20.04`, …). We need to consolidate this onto a **single `ubuntu:24.04` base Docker image** containing **all** system dependencies, and produce, per task instance, a pair of bash scripts (`install.sh` + `eval.sh`) that runs inside that container under a non-root user from an arbitrary CWD, with `uv` pre-installed.

An existing attempt under `dataset_preprocessing/` exists but the user has flagged it as a dead end. We will **ignore that code entirely** and work fresh in a new directory `ubuntu_24_based_tasks/` at the repo root, reusing only the raw inputs:

- `dockerfiles/base_dockerfile/` + `dockerfiles/instance_dockerfile/` (~731 files, source of truth for apt deps and Python setup)
- `run_scripts/<instance_id>/{run_script.sh, parser.py}` (test runners and output parsers — keep as-is)
- HuggingFace dataset `ScaleAI/SWE-bench_Pro` (`instance_id`, `repo`, `base_commit`, `patch`, `before_repo_set_cmd`, `selected_test_files_to_run`, `fail_to_pass`, `pass_to_pass`, etc.)

**First milestone: qutebrowser only** (71 Python instances). Validate end-to-end on one instance, then generalize across qutebrowser, then extend to ansible + openlibrary (the other two Python repos in the dataset).

## Task-runner contract (assumed)

The task runner gives us a container started from our `ubuntu:24.04`-based base image. Inside that container, in some CWD (let's call it `$PWD`), the runner executes per-task:

1. `git clone <repo_url> .` (or extracts a repo tarball into `$PWD`)
2. `git checkout <base_commit>`
3. `bash install.sh`            ← **we provide this** (sets up `.venv/`, installs Python deps)
4. `git apply patch.diff`       ← runner applies the model's patch
5. `bash eval.sh`               ← **we provide this** (runs tests, writes `output.json`, exits 0/1)

Constraints:
- No root. No `/workspace`, `/app`, `/testbed`. Everything happens under `$PWD`.
- `uv` is pre-installed (system PATH).
- `git`, `curl`, `bash` are pre-installed (part of the base image).
- All apt packages live in the base image — `install.sh` does **not** call `apt`.

## Deliverables (first milestone)

A new directory `ubuntu_24_based_tasks/` at the repo root with this layout:

```
ubuntu_24_based_tasks/
├── README.md                      # what this is and how to use it
├── base_image/
│   └── Dockerfile                 # ubuntu:24.04 + uv + git + curl + union of apt deps
├── generate.py                    # CLI: emits install.sh + eval.sh per instance, plus consolidated JSONL
├── templates/
│   ├── install.sh.tmpl            # qutebrowser install template
│   └── eval.sh.tmpl               # qutebrowser eval template
├── apt_packages.txt               # canonical list (union) of apt deps, one per line
├── python_dataset_ubuntu24.jsonl  # NEW: extended dataset (one line per instance)
└── out/
    └── <instance_id>/
        ├── install.sh
        └── eval.sh
```

### Extended JSONL output

`python_dataset_ubuntu24.jsonl` re-emits each input record with these added/overwritten fields:

| Field | Type | Source |
|---|---|---|
| `python_version` | str | parsed from base Dockerfile `FROM python:X.Y-…` (e.g. `"3.9"`, `"3.11"`) |
| `install_script` | str | full text of generated `install.sh` |
| `eval_script` | str | full text of generated `eval.sh` |
| `env_vars` | dict[str,str] | parsed from base + instance Dockerfile `ENV` directives |
| `date_pin` | str \| null | from the `pypi-timemachine YYYY-MM-DD` line in the build heredoc, or null if absent |

All upstream HF fields are preserved verbatim. Consumers can pick either:
- write the embedded `install_script` / `eval_script` strings to disk and run them, or
- reference the on-disk files under `ubuntu_24_based_tasks/out/<iid>/`.

## Per-instance script design

### `install.sh` — runs after checkout, before patch

Reads from the dataset record + per-instance dockerfile build heredoc:

```bash
#!/usr/bin/env bash
set -euo pipefail

# 1. Apply before_repo_set_cmd (resets and selectively checks out test files
#    from the eval commit — needed because base_commit is the "before" state
#    but the eval files come from "after").
git reset --hard <base_commit>
git clean -fd
git checkout <base_commit>
git checkout <eval_commit> -- <selected_test_files>

# 2. Persist task env vars to .swebench_env so eval.sh can re-source them.
#    (install.sh and eval.sh run in separate shells — in-process exports won't survive.)
cat > .swebench_env <<'ENV_EOF'
export PYTEST_ADDOPTS="--tb=short -v --continue-on-collection-errors --reruns=3"
export UV_HTTP_TIMEOUT=60
export UV_EXCLUDE_NEWER=2025-08-26     # date_pin, embedded by generator
export VIRTUAL_ENV="$(pwd)/.venv"
export PATH="$VIRTUAL_ENV/bin:$PATH"
# Repo-specific env (qutebrowser):
export QT_QPA_PLATFORM=offscreen
export PYTEST_QT_API=pyqt5
export QUTE_QT_WRAPPER=PyQt5
ENV_EOF

source .swebench_env

# 3. Create venv in CWD (uv is pre-installed, Python from uv-managed download).
uv venv --python 3.11 .venv     # python_version from base Dockerfile FROM line

# 4. Date-pinned resolver — UV_EXCLUDE_NEWER is already exported above.

# 5. Setuptools strategy (see "Date-pin / setuptools" section below):
#    - For date_pin < 2022-01-01: pin setuptools<60 BEFORE editable installs.
#    - For 2022 ≤ date_pin < 2024: pin setuptools<68.
#    - For date_pin >= 2024 or null: let uv pick latest compatible.
uv pip install "setuptools<60" wheel    # example for an old instance

# 6. Per-instance Python deps, translated from the instance Dockerfile's EOFBUILD body.
#    Strategy: keep the original command list, replace `pip install` with `uv pip install`,
#    strip pypi-timemachine bootstrap lines, keep everything else verbatim.
uv pip install -e .
uv pip install -r misc/requirements/requirements-tests.txt
uv pip install -r misc/requirements/requirements-pyqt.txt
# (or hard-pinned PyQt5/PyQtWebEngine versions for newer instances)

echo "install.sh complete"
```

Key simplifications vs. the previous attempt:
- **No `pypi-timemachine` proxy** — `UV_EXCLUDE_NEWER` does the same job at the resolver level without a background HTTP server.
- **No `apt-get`** — base image handles it.
- **`uv` everywhere** — no system pip.
- **CWD-relative paths only** — `.venv` lives where the runner put us.
- **Env vars persisted to disk** (`.swebench_env`) so eval.sh re-sources them.

### Date-pin / setuptools strategy for old instances

`UV_EXCLUDE_NEWER` makes the resolver act as if today were `<date_pin>` — perfect for runtime deps. The trap is the **build-time** toolchain: when you run `uv pip install -e .` on a 2020-era project, uv invokes whatever `setuptools` happens to be in the build environment, and modern setuptools (>=60 dropped `setup.py develop` semantics, >=68 dropped more) breaks old `setup.py`-only projects.

Mitigation (embedded into the generated `install.sh` based on `date_pin`):

| `date_pin` | Setuptools cap | Why |
|---|---|---|
| < 2021-06-01 | `setuptools<58` | last version with `easy_install`; pre-PEP 517 projects need this |
| < 2022-06-01 | `setuptools<60` | last version with old `develop` cmd path; needed for old `pip install -e .` |
| < 2024-01-01 | `setuptools<68` | safer for projects without `pyproject.toml` |
| >= 2024 or null | (none) | latest |

Generator decides the cap from `date_pin` and emits the right `uv pip install "setuptools<N" wheel` line **before** any `uv pip install -e .`. We also pass `--no-build-isolation` for editable installs on the oldest tier so the pinned setuptools is actually used, rather than uv spinning up an isolated build env with the latest version.

### `eval.sh` — runs after patch is applied

```bash
#!/usr/bin/env bash
# Note: NO `set -e` — test failures must not abort before we parse output.

# Re-source env vars persisted by install.sh.
# This also activates the venv (PATH/VIRTUAL_ENV are baked into .swebench_env).
source .swebench_env

# qutebrowser-specific: headless display (DISPLAY exported here, not persisted —
# Xvfb is per-eval-run lifecycle).
export DISPLAY=:99
Xvfb :99 -screen 0 1024x768x24 &
XVFB_PID=$!
sleep 1

# Write run_script.sh + parser.py from the dataset record (inlined here at generation time)
cat > .swebench_run.sh <<'RUN_EOF'
<contents of run_scripts/<iid>/run_script.sh>
RUN_EOF

cat > .swebench_parser.py <<'PARSER_EOF'
<contents of run_scripts/<iid>/parser.py>
PARSER_EOF

chmod +x .swebench_run.sh

# Selected tests from dataset.selected_test_files_to_run.
bash .swebench_run.sh "tests/unit/utils/test_log.py,tests/unit/utils/test_qtlog.py" \
    > stdout.log 2> stderr.log
TEST_RC=$?

# Parser turns pytest output into output.json: {"tests": [{"name", "status"}, ...]}.
python .swebench_parser.py stdout.log stderr.log output.json

kill "$XVFB_PID" 2>/dev/null || true

# --- Decision snippet: pass iff all expected tests are non-failing. -----------
# fail_to_pass + pass_to_pass are embedded as JSON literals at generation time.
# A test "passes" if its parsed status is in {PASSED, XFAIL, SKIPPED}.
# Statuses FAILED, ERROR, XPASS, or missing-from-output → counted as failure.
python - <<'CHECK_EOF'
import json, sys
EXPECTED = <fail_to_pass + pass_to_pass JSON list, embedded by generator>
OK = {"PASSED", "XFAIL", "SKIPPED"}
try:
    results = {t["name"]: t["status"] for t in json.load(open("output.json"))["tests"]}
except Exception as e:
    print(f"eval.sh: could not read output.json: {e}", file=sys.stderr)
    sys.exit(1)
failed = [n for n in EXPECTED if results.get(n) not in OK]
if failed:
    print(f"eval.sh: {len(failed)}/{len(EXPECTED)} expected tests failed/missing", file=sys.stderr)
    for n in failed[:20]:
        print(f"  - {n}: {results.get(n, 'MISSING')}", file=sys.stderr)
    sys.exit(1)
print(f"eval.sh: all {len(EXPECTED)} expected tests passed")
sys.exit(0)
CHECK_EOF
```

The decision is based on parsed `output.json`, **not** on `$TEST_RC` — pytest's exit code can be polluted by collection errors, teardown warnings, or `--continue-on-collection-errors`. The parser is the source of truth.

## Base image (`ubuntu_24_based_tasks/base_image/Dockerfile`)

```dockerfile
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
    # Core
    bash git curl ca-certificates build-essential pkg-config \
    python3 python3-dev python3-venv python-is-python3 \
    # qutebrowser Qt/X11 deps (from union of all qutebrowser base dockerfiles)
    xvfb dbus-x11 x11-utils xauth \
    libglib2.0-0 libgl1 libegl1 libxkbcommon0 libxkbcommon-x11-0 \
    libdbus-1-3 libnss3 libxss1 libxtst6 libxcomposite1 libxcursor1 \
    libxdamage1 libxext6 libxfixes3 libxi6 libxrandr2 libxrender1 \
    libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
    libxcb-randr0 libxcb-render0 libxcb-render-util0 libxcb-shape0 \
    libxcb-sync1 libxcb-util1 libxcb-xfixes0 libxcb-xinerama0 libxcb-xkb1 \
    libfontconfig1 libfreetype6 fonts-liberation2 fonts-dejavu-core \
    libasound2t64 \
    libxml2-dev libxslt1-dev libffi-dev libssl-dev libyaml-dev \
    # (ansible: same core + libffi-dev — already covered)
    # (openlibrary: + postgresql-client, nodejs, npm — add when extending)
    && rm -rf /var/lib/apt/lists/*

# Install uv to /usr/local/bin so it's on PATH for any user
RUN curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh

# Non-root user (task runner can override, but we provide one)
RUN useradd -m -s /bin/bash runner
USER runner
WORKDIR /home/runner
```

Notes on Ubuntu 24.04 package renames (verified against 24.04 archive):
- `libasound2` → `libasound2t64` (t64 transition)
- `libxslt1.1` → `libxslt1.1` still exists; use the dev-package name for headers
- Qt5 system packages (`libqt5*`, `qtbase5-dev`, etc.) are **dropped**: PyQt5 wheels on PyPI bundle their own Qt, so we don't need system Qt. Confirm during validation.
- `python3-pip` not strictly needed since `uv` handles installs; omit unless something breaks.

## `generate.py` — the converter

A small Python script (~200 lines, no external deps beyond `datasets`) that:

1. Loads `ScaleAI/SWE-bench_Pro` from HF, filters to `repo_language == "python"`, optionally to a single repo (`--repo qutebrowser`) or a single instance (`--instance-id`).
2. For each instance:
   - Reads `dockerfiles/instance_dockerfile/<iid>/Dockerfile` — extracts EOFBUILD body + ENV
   - Reads `dockerfiles/base_dockerfile/<iid>` — extracts `FROM` (→ `python_version`) and ENV
   - Translates EOFBUILD: `pip install` → `uv pip install`; strip `pypi-timemachine` proxy bootstrap; capture `date_pin` from the `pypi-timemachine YYYY-MM-DD` line.
   - Picks the right `setuptools<N` cap from `date_pin` (table above).
   - Reads `run_scripts/<iid>/run_script.sh` and `parser.py` verbatim.
   - Renders `install.sh` and `eval.sh` from templates, embedding fields inline: `base_commit`, `before_repo_set_cmd`, `selected_test_files_to_run`, `fail_to_pass`, `pass_to_pass`, `env_vars`, `date_pin`, `python_version`.
3. Writes:
   - `ubuntu_24_based_tasks/out/<instance_id>/install.sh`
   - `ubuntu_24_based_tasks/out/<instance_id>/eval.sh`
   - Appends one line to `ubuntu_24_based_tasks/python_dataset_ubuntu24.jsonl` with the extended record (original HF fields + `python_version`, `install_script`, `eval_script`, `env_vars`, `date_pin`).

CLI:
```bash
# One instance (dev loop)
python ubuntu_24_based_tasks/generate.py --instance-id instance_qutebrowser__...

# All qutebrowser instances → 71 dirs + 71 JSONL lines
python ubuntu_24_based_tasks/generate.py --repo qutebrowser

# All Python instances (after we extend to ansible + openlibrary)
python ubuntu_24_based_tasks/generate.py --all-python
```

### Files to read during generation
- [dockerfiles/instance_dockerfile/<iid>/Dockerfile](dockerfiles/instance_dockerfile/) — build heredoc + ENV
- [dockerfiles/base_dockerfile/<iid>](dockerfiles/base_dockerfile/) — Python version + ENV
- [run_scripts/<iid>/run_script.sh](run_scripts/) — test runner (inline verbatim into eval.sh)
- [run_scripts/<iid>/parser.py](run_scripts/) — output parser (inline verbatim into eval.sh)

## Verification (first milestone)

Pick the qutebrowser instance the user has been using as smoke-test:
`instance_qutebrowser__qutebrowser-f91ace96223cac8161c16dd061907e138fe85111-v059c6fdc75567943479b23ebca7c07b5e9a7f34c`.

End-to-end manual test:

```bash
# 1. Build the base image
cd ubuntu_24_based_tasks/base_image && docker build -t swebench-pro-ubuntu24 .

# 2. Generate scripts for one instance
cd .. && python generate.py --instance-id instance_qutebrowser__qutebrowser-f91ace96...

# 3. Run the full lifecycle in a container, mimicking the task runner
docker run --rm -it -v $(pwd)/out/instance_qutebrowser__.../:/scripts:ro \
    swebench-pro-ubuntu24 bash -c '
        set -e
        cd ~ && mkdir work && cd work
        git clone https://github.com/qutebrowser/qutebrowser.git .
        git checkout ebfe9b7aa0c4ba9d451f993e08955004aaec4345
        bash /scripts/install.sh
        # Skip patch step for smoke-test "before" baseline
        bash /scripts/eval.sh
        cat output.json
        echo "exit code: $?"
    '
```

Success criteria (smoke test, no patch applied → expect `fail_to_pass` tests to FAIL):
1. `install.sh` completes without error; `.venv/` exists; `qutebrowser` importable.
2. `eval.sh` runs pytest, produces `output.json`, exits non-zero (because `fail_to_pass` tests fail pre-patch).
3. Re-run with the gold patch applied (from dataset's `patch` field) → exits 0.

Full validation per the existing harness:
- Use the gold patch from the HF dataset for the same instance.
- Confirm pre-patch eval → exit 1 and post-patch eval → exit 0.
- Spot-check 3 more qutebrowser instances spanning different `date_pin` values (`2020-11-06`, `2023-09-20`, `2025-08-26`) and different Python versions (3.9 vs 3.11) before declaring qutebrowser done.

## Out of scope (this plan)

- ansible + openlibrary scripts — handled in a follow-up once qutebrowser is green.
- Modal / cloud-runner integration.
- Touching `swe_bench_pro_eval.py` or any other existing code under `dataset_preprocessing/`.
- Pruning the base image. We start with the union of qutebrowser+ansible+openlibrary apt deps so all three repos work against one image.

## Open items to confirm before implementing

- Does the task runner pass the patch path / does it `git apply` before invoking `eval.sh`? Plan assumes yes.
- Is `uv` installed at a fixed path or just on `PATH`? Plan assumes `PATH`.
- Is the runner-provided CWD writable by the user we run as? Plan assumes yes.

---

# Milestone 2: extend to **ansible** (73 instances) — plan

> **Scope lock**: this milestone covers ansible only. Upon approval, I will work exclusively through the steps below, stop at the regression-check gate, and not begin openlibrary or any cleanup work without separate approval.

## Context

Milestone 1 (qutebrowser, 71 instances) is shipped on branch `feature/ubuntu_24_based_tasks` (commit `0354fac`). The toolchain in [ubuntu_24_based_tasks/](ubuntu_24_based_tasks/) — `generate.py` + `validate.py` + base image — runs qutebrowser tasks end-to-end against the itf-demo task runner. Now extending to the second of three Python repos: **ansible** (73 instances).

Empirically from exploration, ansible is **substantially similar** to qutebrowser — same upstream structure (base + instance Dockerfile + EOFBUILD heredoc + pypi-timemachine + ENV directives), same dataset record schema, same `run_script.sh` + `parser.py` pattern. The toolchain should generalise with minimal changes.

## Diffs to expect vs qutebrowser

| Dimension | qutebrowser | ansible | Impact |
|---|---|---|---|
| Python version | 3.8 / 3.9 / 3.11 (3 buckets) | 3.8 / 3.9 / 3.11 / 3.12 (4 buckets) | generate.py extracts from `FROM` line — already works |
| Base image | `python:X.Y-slim` | mostly `python:X.Y-slim`; **a few use `ubuntu:20.04`** (system python3.9) | `parse_python_version()` regex (`python:(\d+\.\d+)`) won't match `ubuntu:20.04` → will fail. Need fallback. |
| Apt deps | Qt5/X11 stack (~50 pkgs) | git, build-essential, libffi-dev, libssl-dev, openssh-client, sshpass, rsync | Already in `base_image/Dockerfile` (covered by the qutebrowser-superset). One missing: **`sshpass`**, **`rsync`** — add. |
| Test runner | pytest + Xvfb + dbus-run-session | `bin/ansible-test` (custom) OR pytest, depending on instance | run_script.sh handles both already; no change needed in generator. |
| Headless GUI | Required (Xvfb) | NOT needed | **Generator must stop hardcoding Xvfb in eval.sh** |
| Editable install | `uv pip install -e .` | mix: `pip install .`, `pip install -e .`, `python setup.py develop` | `setup.py develop` is a third path — need to verify our `--no-build-isolation` logic handles it. |
| date_pin range | 2020-05 → 2025-08 | 2020-04 → 2025-08 | Same range. Existing `setuptools_cap()` table covers it. |
| `before_repo_set_cmd` | strips test-checkout (we do that) | same shape | no change |

## Required code changes

### 1. `generate.py`

**A. Conditional Xvfb in EVAL_TMPL** — currently lines 264–268 unconditionally start Xvfb. New approach: a heuristic at generation time that scans the per-instance `run_script.sh` and emits the Xvfb startup + teardown block only when needed.

```python
# In render_eval(...):
needs_xvfb = bool(re.search(r"\b(Xvfb|QT_QPA_PLATFORM|DISPLAY=)", run_script))
xvfb_start = (
    'export DISPLAY=:99\n'
    'Xvfb :99 -screen 0 1024x768x24 >/dev/null 2>&1 &\n'
    'XVFB_PID=$!\n'
    'sleep 1\n'
) if needs_xvfb else ''
xvfb_stop = 'kill "$XVFB_PID" 2>/dev/null || true\n' if needs_xvfb else ''
```

Then thread `@@XVFB_START@@` and `@@XVFB_STOP@@` placeholders through the template (replacing the hardcoded block). All qutebrowser instances will still get Xvfb; all ansible instances won't.

**B. `parse_python_version()` fallback for `ubuntu:20.04` bases** — ansible has ~4 instances whose base Dockerfile is `FROM ubuntu:20.04` and installs `python3.9` via apt. Current regex `python:(\d+\.\d+)` fails on these. Fix: after the current regex, try a second regex matching `python3\.(\d+)` from apt-get lines (these instances apt-install `python3.9`, `python3.9-dev`, etc.). If still no match, default to `3.11` and emit a warning to stderr.

**C. (Maybe) handle `python setup.py develop`** — one ansible instance variant uses this instead of `pip install -e .`. Current `--no-build-isolation` logic only triggers on `uv pip install -e .`. We'd need to either (i) rewrite `python setup.py develop` → `uv pip install --no-build-isolation -e .`, or (ii) leave it — `setup.py develop` invokes the venv's python directly, which has the pinned setuptools. Probably (ii) is fine. **Verify during smoke test; only fix if it breaks.**

### 2. `base_image/Dockerfile`

Add **`sshpass`** and **`rsync`** to the apt-get install list. The rest of ansible's deps (git, build-essential, libffi-dev, libssl-dev, openssh-client, python-is-python3) are already there.

### 3. `validate.py`

Already has ansible in `REPO_URLS`. No change needed.

## Files to modify

- [ubuntu_24_based_tasks/generate.py](ubuntu_24_based_tasks/generate.py) — EVAL_TMPL + `render_eval()` for conditional Xvfb; `parse_python_version()` for ubuntu:20.04 fallback.
- [ubuntu_24_based_tasks/base_image/Dockerfile](ubuntu_24_based_tasks/base_image/Dockerfile) — append `sshpass`, `rsync` to apt list.
- [ubuntu_24_based_tasks/README.md](ubuntu_24_based_tasks/README.md) — update "Status" table to show ansible as in-progress / done.

No new files needed. `python_dataset_ubuntu24.jsonl` grows from 71 → 144 records.

## Execution steps

1. **Modify `generate.py`** — conditional Xvfb (heuristic on run_script content); python_version fallback.
2. **Modify `base_image/Dockerfile`** — add `sshpass`, `rsync`.
3. **Rebuild base image:** `docker build -t swebench-pro-ubuntu24 ubuntu_24_based_tasks/base_image/`
4. **Generate scripts for both repos:** `python ubuntu_24_based_tasks/generate.py --repo qutebrowser` then `--repo ansible`. (Or, after adding `--all-python` support, do it in one call. Currently `generate.py` already supports `--all-python`.) Verify the JSONL has 144 records; spot-check that **qutebrowser eval.sh files still contain Xvfb** and **ansible eval.sh files do NOT**.
5. **Pick a smoke-test ansible instance** (something simple, recent date_pin, python 3.11): inspect the dataset, pick e.g. an instance from 2023 or 2024 with `python:3.11-slim` base. Run `python ubuntu_24_based_tasks/validate.py --instance-id <iid>`. Expect pre_rc=1, post_rc=0.
6. **Spot-check 3 diverse ansible instances** spanning the Python-version × date_pin matrix:
   - One Python 3.8 instance (oldest)
   - One Python 3.9 instance, old date_pin (e.g. 2020-06)
   - One Python 3.12 instance (newest — only 1 to choose from)
7. **Iterate on failures** — for each failing instance, inspect the live container log via the validator's streaming output. Likely fix points: `setup.py develop` rewrite, ubuntu:20.04 base python detection, edge cases in DROP_LINE_RES.

## Regression check (final gate)

Run the qutebrowser smoke + spot-check set with the updated `generate.py` to confirm no regression:
```
python ubuntu_24_based_tasks/validate.py --repo qutebrowser --index 0,24,28,60
```
Expect: all four ✓ pre_rc=1 post_rc=0, same as the milestone 1 baseline.

## Verification matrix (success criteria)

| Test | Expectation |
|---|---|
| `generate.py --repo ansible` | Emits 73 `install.sh` + 73 `eval.sh` + 73 JSONL records, no exceptions |
| Smoke-test ansible instance | pre_rc=1, post_rc=0 |
| 3 diverse ansible instances | all ✓ pre/post |
| qutebrowser regression (index 0,24,28,60) | all 4 ✓ pre/post |
| qutebrowser eval.sh files contain Xvfb stanza | yes (regex check) |
| ansible eval.sh files contain Xvfb stanza | no (regex check) |
| `python_dataset_ubuntu24.jsonl` | 144 lines total, all fields populated |

## Out of scope for this milestone

- openlibrary (91 instances) — milestone 3 (planned below; not started without separate approval).
- Unifying apt-deps union back into itf-demo's `install_swe_deps.sh` — wait until all three repos are validated.
- Pruning the base image (removing Qt5/X11 stack once ansible+openlibrary are done and we know what's truly needed) — separate cleanup pass at the end.

---

# Milestone 3: extend to **openlibrary** (91 instances) — plan

> **Scope lock**: this milestone covers openlibrary only. It is **not started until milestone 2 (ansible) is green and the user separately approves milestone 3**. Listed here so the overall plan is visible end-to-end.

## Context

After milestone 2 lands ansible alongside qutebrowser, the final Python repo is **internetarchive/openlibrary** (91 instances — the largest of the three). Unlike ansible, openlibrary is **structurally further from qutebrowser** — it has more idiosyncratic build steps (git submodules, `make` targets, npm install, hardcoded `/app`-prefixed PYTHONPATH/OL_CONFIG, an `infogami` symlink). The toolchain needs concrete adjustments, not just code-path additions.

## Diffs to expect vs qutebrowser + ansible

| Dimension | qutebrowser | ansible | openlibrary | Impact |
|---|---|---|---|---|
| Python version | 3.8/3.9/3.11 | 3.8/3.9/3.11/3.12 | **3.12 uniformly** | trivially handled |
| Base image | `python:X.Y-slim` | `python:X.Y-slim` / `ubuntu:20.04` | `python:3.12.2-slim-bookworm` via ECR mirror | `parse_python_version` already matches `python:3.12.2` |
| Apt deps | Qt5/X11 stack | sshpass, rsync | **`postgresql-client`, `libpq-dev`, `libxml2-dev`, `libxslt-dev`, `parallel`, `nodejs`, `npm`** | most already in base image; double-check union after generate. |
| Git submodules | none | none | **`git submodule update --init --recursive`** in base Dockerfile | install.sh must run submodule init after `git reset --hard <base_commit>`. Currently `before_repo_set_cmd` does NOT include this; generator must inject. |
| Build steps | pip installs only | pip installs only | **`npm ci`, `make git/css/js/components/i18n`, `ln -sf vendor/infogami/infogami infogami`** | translate_build_body() must pass these through verbatim (not pip-rewrite). Make-targets that fail with `\|\| true` already get preserved by the current logic. |
| Hardcoded `/app` env | none | `PYTHONPATH=/app` (rewritten downstream) | **`PYTHONPATH=/app`, `OL_CONFIG=/app/conf/openlibrary.yml`** in BOTH the build heredoc AND the run_script.sh | `/app` doesn't exist at runtime. The build-body lines set these as `export` (won't propagate past install.sh; not a problem). But run_script.sh hardcodes `/app` paths — **needs rewriting** at generation time. |
| Test runner | pytest + Xvfb | ansible-test/pytest, no Xvfb | pytest, no Xvfb | conditional-Xvfb heuristic from milestone 2 already covers this. |
| Repo size / build time | ~3 min | ~3 min | longer — npm + make steps add ~5–10 min per install | longer validation runs; consider `--skip-post-patch` for spot-checks. |

## Required code changes

### 1. `generate.py`

**A. Inject `git submodule update --init --recursive` after the repo reset.** Detect from the base Dockerfile (the line `RUN git submodule update --init --recursive` is the signal). If present, append it to the install.sh's step-1 block after `before_repo_set_cmd`.

**B. Rewrite `/app` references in run_script.sh.** When inlining the run_script content into eval.sh, replace `/app` with `$(pwd)` in `export PYTHONPATH=...` and `export OL_CONFIG=...` (or — simpler — replace `=/app/` with `="$(pwd)/"` globally in the inlined content). Confirm this doesn't break the qutebrowser/ansible run_scripts (they shouldn't contain `/app` paths at all; verify).

**C. Pass-through for non-pip build commands.** `npm ci`, `make ...`, `ln -sf` — current translate_build_body only special-cases `pip install` and a few drops. These commands should already pass through verbatim. **Verify during smoke test.** If `make` is followed by a target that needs e.g. `nodejs` not present in the venv PATH, that's a base-image concern, not a generator concern.

### 2. `base_image/Dockerfile`

Confirm these are already present (from milestone 1's union):
- `postgresql-client`, `libpq-dev`, `libxml2-dev`, `libxslt1-dev` (note: 24.04 uses `libxslt1-dev` not `libxslt-dev`), `parallel`, `zip`, `unzip`, `nodejs`, `npm`, `make`.

Add any that are missing. Specifically `make` may be needed (not currently explicit) — check.

### 3. `validate.py`

Add `internetarchive/openlibrary` → `https://github.com/internetarchive/openlibrary.git` to `REPO_URLS` if not already there.

Also, since the repo clone needs `--recurse-submodules` to fully replicate the upstream setup, decide whether validate.py should do that at clone time OR rely on install.sh to run submodule init. Current install.sh approach (step A above) is preferable because it matches the task-runner contract — the runner clones, then runs install.sh.

## Files to modify

- [ubuntu_24_based_tasks/generate.py](ubuntu_24_based_tasks/generate.py) — submodule injection, `/app` rewrite in inlined run_script.
- [ubuntu_24_based_tasks/base_image/Dockerfile](ubuntu_24_based_tasks/base_image/Dockerfile) — confirm/add openlibrary's apt deps + `make`.
- [ubuntu_24_based_tasks/validate.py](ubuntu_24_based_tasks/validate.py) — REPO_URLS entry.
- [ubuntu_24_based_tasks/README.md](ubuntu_24_based_tasks/README.md) — status table.

## Execution steps

1. Modify `generate.py` (submodule injection, `/app` rewrite).
2. Update `base_image/Dockerfile` (apt additions, if any).
3. Rebuild base image.
4. `python ubuntu_24_based_tasks/generate.py --all-python` — now produces all 235 records (qutebrowser 71 + ansible 73 + openlibrary 91). Spot-check three sample eval.sh files: a qutebrowser one (has Xvfb), an ansible one (no Xvfb, no `/app` rewrites), an openlibrary one (no Xvfb, **`PYTHONPATH=$(pwd)`** instead of `=/app`).
5. Pick a smoke-test openlibrary instance (recent date_pin, well-known commit). Run `validate.py --instance-id <iid>`. Expect pre_rc=1, post_rc=0. Expect a longer total runtime (~10–15 min for install+eval).
6. Spot-check 3 diverse openlibrary instances across the date_pin spread.

## Regression check (final gate)

Combined re-run across all three repos:
```
python ubuntu_24_based_tasks/validate.py --repo qutebrowser --index 0,24,28,60
python ubuntu_24_based_tasks/validate.py --repo ansible --index <to-pick during milestone 2>
python ubuntu_24_based_tasks/validate.py --repo openlibrary --index <to-pick during milestone 3>
```
Expect all ✓ pre_rc=1 post_rc=0.

## Verification matrix (success criteria)

| Test | Expectation |
|---|---|
| `generate.py --repo openlibrary` | 91 install.sh + eval.sh + 91 JSONL records, no exceptions |
| openlibrary install.sh contains `git submodule update --init --recursive` | yes |
| openlibrary eval.sh's inlined run_script has `PYTHONPATH=$(pwd)` (not `/app`) | yes |
| Smoke-test openlibrary instance | pre_rc=1, post_rc=0 |
| 3 diverse openlibrary instances | all ✓ |
| qutebrowser regression (index 0,24,28,60) | all 4 ✓ |
| ansible regression (4 instances picked in milestone 2) | all 4 ✓ |
| `python_dataset_ubuntu24.jsonl` | 235 lines total, all fields populated |

## Out of scope for this milestone

- Unifying apt-deps union back into itf-demo's `install_swe_deps.sh` — happens **after** milestone 3 succeeds, as a separate PR exercise.
- Base-image pruning — same; defer until the full 235-instance set is validated and we know which packages are truly needed.
- Anything outside `ubuntu_24_based_tasks/` — no edits to `dataset_preprocessing/`, `swe_bench_pro_eval.py`, etc.
