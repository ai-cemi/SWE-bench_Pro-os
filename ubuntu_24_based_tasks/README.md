# `ubuntu_24_based_tasks/` — SWE-bench Pro Python tasks on plain `ubuntu:24.04`

Replaces the per-instance Docker images shipped upstream with:
- **one** `ubuntu:24.04` base image holding all system deps + `uv`, and
- a pair of bash scripts per task (`install.sh` + `eval.sh`) that run inside that container under a non-root user from an arbitrary CWD.

The goal is to plug directly into the `itf-demo` task runner ([crates/workload_agentic_coding_runner](../../itf-demo/crates/workload_agentic_coding_runner/)) without per-instance Docker builds.

## Status

| Repo | Instances | Validated |
|---|---|---|
| qutebrowser | 79 | smoke + spot-check (4/79 across Python 3.8/3.9/3.11, date_pin 2020-05 → 2025-08) |
| ansible | 96 | smoke + spot-check (4/96 across Python 3.8/3.9/3.11/3.12, incl. `ubuntu:20.04` base case) |
| openlibrary | 91 | — |
| **Total Python** | **266** | 175 generated (qutebrowser + ansible), openlibrary pending |

## Files

| Path | Purpose |
|---|---|
| `generate.py` | Parses upstream base + instance Dockerfiles, emits `install.sh` + `eval.sh` per instance and an extended JSONL |
| `validate.py` | End-to-end validation driver (clones repo, runs install+test_patch+eval inside the base image, live-streams docker output) |
| `base_image/Dockerfile` | The `ubuntu:24.04` + `uv` + union-of-apt-deps reference image |
| `raw/raw_python_dataset.jsonl` | **Source of truth**: Python subset of `ScaleAI/SWE-bench_Pro` from HuggingFace (266 records). Refresh with `python -c "from datasets import load_dataset; ..."` if upstream changes. |
| `python_dataset_ubuntu24.jsonl` | **Standalone output**: one line per generated instance with `setup_script`, `eval_scripts`, `env_vars`, `python_version`, `date_pin` (+ all upstream HF fields). The task runner needs nothing else. |
| `out/<instance_id>/{install,eval}.sh` | Same content as the JSONL fields, written to disk as files for easier inspection / standalone runs |

## Task-runner contract

Mirrors `workload_agentic_coding_runner/src/runner.rs` + `evaluator.rs`. Per task, the runner does:

1. `git clone <repo_url> <workspace_dir>` (then `git checkout <base_commit>`).
2. `bash setup_script` (CWD = workspace_dir).
3. Harness runs (model produces patch and applies it in-place).
4. `git apply --whitespace=fix <test_patch>`.
5. For each `eval_script` in `eval_scripts`: `bash eval_script` (CWD = workspace_dir, 1800s timeout). **Success = exit code 0.**

Field names match the protocol crate (`TaskDescription` in `workload_agentic_coding_protocol::models`): `setup_script: String`, `eval_scripts: Vec<String>`, `test_patch: String`, `patch: String`.

### What `install.sh` does

1. Apply `before_repo_set_cmd` (`git reset --hard <base_commit>`, etc.). Test-file checkout lines are stripped because the runner applies `test_patch` separately.
2. Export static env vars from base + instance Dockerfile `ENV` directives, plus `UV_EXCLUDE_NEWER=<date_pin>`, `VIRTUAL_ENV`, `PATH`.
3. `uv venv --python <X.Y> .venv`.
4. Install setuptools + wheel with the right cap (see below).
5. Run the per-instance install steps (translated from the upstream Dockerfile EOFBUILD heredoc: `pip install` → `uv pip install`, `pypi-timemachine` proxy stripped).
6. Dump the resulting environment to `.swebench_env` (one safe `export K='V'` line per variable, with shell-managed vars denylisted) so `eval.sh` can re-source it.

### What `eval.sh` does

1. `source .swebench_env`.
2. **Conditionally** start `Xvfb` — only when the inlined `run_script.sh` references `Xvfb` / `QT_QPA_PLATFORM` / `DISPLAY=`. Qutebrowser triggers this; ansible/openlibrary do not.
3. Inline the run_script.sh and parser.py from `run_scripts/<iid>/`. `/app` paths in the inlined content are rewritten to `"$(pwd)"` at generation time, so the scripts work from the runner's CWD.
4. Run the project's test entrypoint with `selected_test_files_to_run`, capture stdout/stderr.
5. Parse output into `output.json`.
6. **Scorer**: exit 0 iff `output.json` has no `FAILED` or `ERROR` entries — exit 1 otherwise. Pytest exit code is ignored on purpose (collection errors and Qt teardown crashes pollute it).

### Setuptools / editable-install strategy

Old projects break in two ways:
- **`uv pip install -e .` needs setuptools >= 64** for PEP 660 `build_editable`.
- **Old `setup.py` projects break on setuptools >= 68** which removed `easy_install` / `develop` command paths.

Compromise (`generate.py: setuptools_cap()`):

| `date_pin` | setuptools | extras |
|---|---|---|
| < 2022-06-01 | `>=64,<67` | `--no-build-isolation` for `uv pip install -e .` + `--exclude-newer-package=setuptools=2024-01-01` to bypass the date cutoff on setuptools itself |
| < 2024-01-01 | `>=64,<68` | `--exclude-newer-package=setuptools=2024-01-01` |
| >= 2024 (or none) | latest | — |

## Quick start

```bash
# 1. Build the base image (one-time, ~3 minutes)
docker build -t swebench-pro-ubuntu24 ubuntu_24_based_tasks/base_image/

# 2. (One-time / on upstream change) Refresh raw HF dataset
python -c "
from datasets import load_dataset
import json
ds = load_dataset('ScaleAI/SWE-bench_Pro', split='test')
with open('ubuntu_24_based_tasks/raw/raw_python_dataset.jsonl','w') as f:
    for r in ds:
        if r.get('repo_language') == 'python':
            f.write(json.dumps(r) + '\n')
"

# 3. Generate scripts for one or more repos
python ubuntu_24_based_tasks/generate.py --repo qutebrowser   # 79 instances
python ubuntu_24_based_tasks/generate.py --repo ansible       # 96 instances (appends to JSONL)
# -> ubuntu_24_based_tasks/out/<iid>/{install.sh,eval.sh} and ubuntu_24_based_tasks/python_dataset_ubuntu24.jsonl

# 4. Validate one instance end-to-end (clone, install, gold patch, test_patch, eval)
python ubuntu_24_based_tasks/validate.py --instance-id <iid>

# Or pick by index inside a repo (0..78 for qutebrowser, 0..95 for ansible)
python ubuntu_24_based_tasks/validate.py --repo qutebrowser --index 0
python ubuntu_24_based_tasks/validate.py --repo qutebrowser --index 0,5,42

# Random sample (deterministic seed)
python ubuntu_24_based_tasks/validate.py --repo qutebrowser --sample 5

# One per (python_version, date_pin) bucket — slow, exhaustive
python ubuntu_24_based_tasks/validate.py --repo qutebrowser --diverse

# Faster smoke check (skip the gold-patch run)
python ubuntu_24_based_tasks/validate.py --repo qutebrowser --sample 3 --skip-post-patch
```

`validate.py` streams the container's stdout+stderr to your terminal in real time, prefixed with `[pre]` or `[post]` so you can follow what's happening. Per instance it asserts:
- Pre-patch (no harness): scorer exits **1** (`fail_to_pass` tests fail, `pass_to_pass` tests pass).
- Post-patch (gold harness): scorer exits **0** (all tests pass).

## Known flaky failures (Python stdlib drift)

A small fraction of instances may show `unresolved` results not because the model's patch is wrong, but because **Python stdlib behavior drifted between the patch version baked into the upstream `python:X.Y-slim` Docker image and the patch version `uv` installs today**. The upstream Docker base tags (e.g. `python:3.9-slim`) track the *latest* 3.9.x at build time, so even the original eval setup wasn't deterministic across re-runs.

Example: `instance_ansible__ansible-b748edea…` (date_pin 2020-05-20, Python 3.9) — the test `test_prepare_multipart` compares a multipart HTTP body with `\n` line endings, but Python 3.9.5+ ([bpo-43124](https://bugs.python.org/issue43124)) changed `email.generator.BytesGenerator` to emit `\r\n`. Upstream's Docker shipped pre-3.9.5; `uv` installs CPython 3.9.25 today. Same code, same patch, **different test fixture expectation**.

Symptom: 1 test in `fail_to_pass` (or rarely `pass_to_pass`) fails post-patch with a `\n` vs `\r\n` byte-string diff or similar low-level stdlib-behavior diff. Pre-patch run also produces a wrong `pre_rc=0` (because the test passes due to the stdlib bug-fix rather than the model's work).

Not in scope to fix at the toolchain level — would require pinning `uv venv --python X.Y.Z` to a specific patch matched by date_pin, and uv's older patch builds carry their own bug surface. Accepted as ambient noise (~3-5% of py 3.8 / 3.9 instances).

## End-goal

After qutebrowser + ansible + openlibrary are all validated:

1. Compute the union of apt deps across all three (235 instances).
2. PR that union back to `itf-demo`'s [install_swe_deps.sh](../../itf-demo/crates/workload_agentic_coding_runner/install_swe_deps.sh).
3. Drop `ubuntu_24_based_tasks/base_image/Dockerfile` (or keep as a standalone-test reference).
4. Ship `python_dataset_ubuntu24.jsonl` as the canonical Python task dataset for the runner.
