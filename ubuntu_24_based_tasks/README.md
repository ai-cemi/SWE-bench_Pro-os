# `ubuntu_24_based_tasks/` — SWE-bench Pro Python tasks on plain `ubuntu:24.04`

Replaces the per-instance Docker images shipped upstream with:
- **one** `ubuntu:24.04` base image holding all system deps + `uv`, and
- a pair of bash scripts per task (`install.sh` + `eval.sh`) that run inside that container under a non-root user from an arbitrary CWD.

The dataset (`python_dataset_ubuntu24.jsonl`) carries everything the task runner needs per instance — `setup_script`, `eval_scripts`, `patch`, `test_patch`, `python_version`, `date_pin`, `env_vars`, `oracle_check_ok` — so it plugs into the `itf-demo` task runner ([crates/workload_agentic_coding_runner](../../itf-demo/crates/workload_agentic_coding_runner/)) without per-instance Docker builds.

---

## Status

Last full sweep through the NATS-based oracle harness:

| Repo        | Instances | Resolved | %     |
|-------------|----------:|---------:|------:|
| ansible     |        96 |    83/96 | 86.5% |
| qutebrowser |        79 |    66/79 | 83.5% |
| openlibrary |        91 |    84/91 | 92.3% |
| **Total**   |   **266** | **233/266** | **87.6%** |

`oracle_check_ok=True` in `python_dataset_ubuntu24.jsonl` iff the runner reported `resolved: True` AND there was no harness/transport error for that instance.

---

## File tree

```
ubuntu_24_based_tasks/
├── README.md                          # this file
├── generate.py                        # source of truth: builds install.sh + eval.sh + dataset
├── validate.py                        # standalone single-instance debugger (one docker run, no NATS)
├── base_image/
│   └── Dockerfile                     # union of apt deps the runner image must ship
├── raw/
│   └── raw_python_dataset.jsonl       # frozen HF dump (input to generate.py)
├── python_dataset_ubuntu24.jsonl      # output: 266 records, annotated with oracle_check_ok
├── out/<instance_id>/                 # rendered install.sh + eval.sh per instance (gitignored)
└── task_runner_testing/
    ├── run_dataset.py                 # canonical NATS client (sequential, foreground)
    ├── retry_run.py                   # re-run a subset (CLI flags for dataset/results/task)
    ├── merge_retry.py                 # fold a retry result file into a larger one
    ├── stats.py                       # bucket-by-reason classifier for any result file
    ├── task.json                      # client config skeleton (run_id, harness, endpoint)
    ├── dataset.qute_ansible.jsonl     # 175-row dataset subset (batch 1 input)
    ├── dataset.openlibrary.jsonl      # 91-row dataset subset (batch 2 input)
    ├── results.qute_ansible.jsonl     # batch-1 raw output
    ├── results.openlibrary.jsonl      # batch-2 raw output
    └── results.jsonl                  # canonical merged output, in dataset order
```

---

## Task-runner contract

Mirrors `workload_agentic_coding_runner/src/runner.rs` + `evaluator.rs`. Per task, the runner does:

1. `git clone <repo_url> <workspace_dir>` (then `git checkout <base_commit>`).
2. `bash setup_script` (CWD = workspace_dir).
3. Harness runs (model produces a patch and applies it in-place — for the oracle harness this is the dataset's gold `patch`).
4. `git apply --whitespace=fix <test_patch>`.
5. For each entry in `eval_scripts`: `bash <eval_script>` (CWD = workspace_dir, 1800s timeout). **Success = exit code 0.**

Field names match the protocol crate (`TaskDescription` in `workload_agentic_coding_protocol::models`): `setup_script: String`, `eval_scripts: Vec<String>`, `test_patch: String`, `patch: String`.

### What `install.sh` does

1. Apply `before_repo_set_cmd` (`git reset --hard <base_commit>`, etc.). Test-file checkout lines are stripped because the runner applies `test_patch` separately.
2. Initialize git submodules with `git://` → `https://` rewrite (GitHub disabled the git protocol in 2022; old `.gitmodules` files still pin `git://github.com/…` URLs that would hang forever otherwise). Needed for openlibrary's `vendor/infogami`.
3. Export static env vars from base + instance Dockerfile `ENV` directives, plus `TZ=UTC`, `UV_EXCLUDE_NEWER=<date_pin>`, `VIRTUAL_ENV`, `PATH`.
4. `uv venv --seed --python <X.Y> .venv`. `--seed` is essential: pip/setuptools/wheel get installed into the venv. Required by anything that shells out to `python -m pip` inside the venv at eval time (e.g. ansible-test).
5. Install setuptools + wheel with the right cap (see [Setuptools / editable-install strategy](#setuptools--editable-install-strategy)).
6. Run the per-instance install steps (translated from the upstream Dockerfile `EOFBUILD` heredoc: `pip install`/`pip3 install`/`python -m pip install` → `uv pip install`, `pypi-timemachine` proxy stripped, `--default-timeout` flag stripped, `npm install` and `make` lines made tolerant via `|| true`).
7. Dump the resulting environment to `.swebench_env` (one safe `export K='V'` line per variable, with shell-managed vars denylisted) so `eval.sh` can re-source it.

### What `eval.sh` does

1. `source .swebench_env`.
2. **Conditionally** start `Xvfb` — only when the inlined `run_script.sh` references `Xvfb` / `QT_QPA_PLATFORM` / `DISPLAY=`. Qutebrowser triggers this; ansible/openlibrary do not.
3. Inline the run_script.sh and parser.py from `run_scripts/<iid>/`. `/app` paths in the inlined content are rewritten to `"$(pwd)"` at generation time, so the scripts work from the runner's CWD. Pytest invocations get `--override-ini="filterwarnings="` injected so that a project's `pytest.ini` setting `filterwarnings = error` doesn't turn unrelated third-party `DeprecationWarning`s into fatal conftest-import failures (with a hand-curated opt-out list for tests that specifically *rely* on `pytest.warns`).
4. Run the project's test entrypoint with `selected_test_files_to_run`, capture stdout/stderr.
5. Parse output into `output.json`.
6. **Scorer**: exit 0 iff `output.json` has no `FAILED` or `ERROR` entries — exit 1 otherwise. Pytest exit code is ignored on purpose (collection errors and Qt teardown crashes pollute it).

### Setuptools / editable-install strategy

Old projects break in two ways, and setuptools 69+ introduced a third:

- **`uv pip install -e .` needs setuptools ≥ 64** for PEP 660 `build_editable`.
- **Old `setup.py` projects break on setuptools ≥ 68**, which removed `easy_install` / `develop` command paths.
- **setuptools ≥ 69 imports `jaraco.functools.splat`** in `_distutils/_modified.py`. Projects pinning `jaraco.functools < 4` (qutebrowser does at multiple base_commits) trigger an `ImportError` at venv runtime — any subsequent `import distutils.*` (hunter, setuptools' own `_distutils_hack` precedence) crashes.

Compromise (`generate.py: setuptools_cap()`):

| `date_pin` | setuptools | extras |
|---|---|---|
| `< 2022-06-01` | `>=64,<67` | `--no-build-isolation` for `uv pip install -e .` + `--exclude-newer-package=setuptools=2024-01-01` to bypass the date cutoff on setuptools itself |
| `≥ 2022-06-01` (or `None`) | `>=64,<69` | `--exclude-newer-package=setuptools=2024-01-01` when `date_pin` is set; build isolation enabled |

The `<69` ceiling is always applied — even for modern date_pins — because the splat dependency is independent of the date cutoff.

---

## Workflow

The end-to-end workflow is three sequential steps: **generate** the per-instance scripts + dataset, **run** them through the NATS oracle harness, **merge + annotate** the results back onto the dataset.

### Prereqs

- `itf-demo` task runner running on docker-compose. NATS on host port 4222 with auth `natsuser` / `natspassword`.
- The runner image must include the apt set from `base_image/Dockerfile`. The actual deployed image lives at `itf-demo/crates/workload_agentic_coding_runner/Dockerfile`; **`base_image/Dockerfile` here is the single edit point for the apt deps our install scripts need**. When apt deps change: edit `base_image/Dockerfile` first (focused diff), then mirror the apt section into the itf-demo Dockerfile, then rebuild + restart the runner.
- Local `docker` CLI (the client shells out to `natsio/nats-box:latest` for sub/pub).
- Python 3.10+ on the host (stdlib only).

### Step 1 — Generate the dataset

```bash
# 1a. Build the base image (one-time, ~3 minutes)
docker build -t swebench-pro-ubuntu24 ubuntu_24_based_tasks/base_image/

# 1b. (One-time / on upstream change) Refresh raw HF dataset
python -c "
from datasets import load_dataset
import json
ds = load_dataset('ScaleAI/SWE-bench_Pro', split='test')
with open('ubuntu_24_based_tasks/raw/raw_python_dataset.jsonl','w') as f:
    for r in ds:
        if r.get('repo_language') == 'python':
            f.write(json.dumps(r) + '\n')
"

# 1c. Generate scripts for all repos (266 instances) or one at a time
python ubuntu_24_based_tasks/generate.py --all-python
# or:
python ubuntu_24_based_tasks/generate.py --repo qutebrowser
python ubuntu_24_based_tasks/generate.py --repo ansible
python ubuntu_24_based_tasks/generate.py --repo openlibrary
# -> ubuntu_24_based_tasks/out/<iid>/{install.sh,eval.sh} and
# -> ubuntu_24_based_tasks/python_dataset_ubuntu24.jsonl
```

The `out/` directory is gitignored; everything you need to feed the runner lives in `python_dataset_ubuntu24.jsonl`.

### Step 2 — Run through the NATS task runner

All commands below run from `task_runner_testing/`.

```bash
# 2a. Build the batch subsets
python3 -c "
import json
qa = open('task_runner_testing/dataset.qute_ansible.jsonl', 'w')
ol = open('task_runner_testing/dataset.openlibrary.jsonl', 'w')
for line in open('python_dataset_ubuntu24.jsonl'):
    iid = json.loads(line)['instance_id']
    if iid.startswith(('instance_ansible__','instance_qutebrowser__')):
        qa.write(line)
    elif iid.startswith('instance_internetarchive__'):
        ol.write(line)
"

# 2b. Run
cd task_runner_testing/

# Batch 1: qutebrowser + ansible (~2h)
python3 run_dataset.py --dataset dataset.qute_ansible.jsonl \
    --results results.qute_ansible.jsonl --timeout 1200

# Batch 2: openlibrary (~1.5h)
python3 run_dataset.py --dataset dataset.openlibrary.jsonl \
    --results results.openlibrary.jsonl --timeout 1200
```

Each row is appended as it completes; resume with `--start-from N`.

### Step 3 — Merge + annotate

```bash
# 3a. Backup the dataset first (it'll be rewritten in place)
cp ../python_dataset_ubuntu24.jsonl ../python_dataset_ubuntu24.jsonl.bak

# 3b. Merge per-batch result files into one results.jsonl in dataset order
python3 -c "
import json
res = {}
for p in ('results.qute_ansible.jsonl', 'results.openlibrary.jsonl'):
    for line in open(p):
        r = json.loads(line); res[r['instance_id']] = r
with open('../python_dataset_ubuntu24.jsonl') as f, open('results.jsonl','w') as g:
    for line in f:
        iid = json.loads(line)['instance_id']
        if iid in res: g.write(json.dumps(res[iid]) + '\n')
"

# 3c. Annotate oracle_check_ok on the dataset
python3 -c "
import json, os
res = {json.loads(l)['instance_id']: json.loads(l) for l in open('results.jsonl')}
def ok(r):
    if r is None or r.get('error'): return False
    raw = r.get('raw') or {}
    if raw.get('error'): return False
    return bool(r.get('resolved'))
src = '../python_dataset_ubuntu24.jsonl'
tmp = src + '.tmp'
with open(src) as fin, open(tmp,'w') as fout:
    for line in fin:
        row = json.loads(line)
        row['oracle_check_ok'] = ok(res.get(row['instance_id']))
        fout.write(json.dumps(row) + '\n')
os.replace(tmp, src)
"
```

### Result file schema

Every row in `results*.jsonl`:

```json
{
  "instance_id": "instance_<org>__<repo>-<commit>-v<setup-hash>",
  "resolved": true,                      // null if error=true
  "error": false,                        // true if harness never replied
  "raw": {                               // null if error=true
    "run_id": "...",
    "task_id": "<instance_id>",
    "success": true,                     // false on harness-side errors
    "error": "setup script failed (exit 2): …",  // present on setup failure
    "evaluation": {                      // present iff success=true
      "resolved": true,
      "eval_script_results": [
        {"name": "0", "passed": true, "output": "eval.sh: …"}
      ],
      "total_scripts_run": 1
    },
    "started_at": "2026-05-13T08:00:00Z",
    "finished_at": "2026-05-13T08:01:11Z"
  }
}
```

### Helper scripts (under `task_runner_testing/`)

- **`run_dataset.py`** — the canonical runner. Sequential, foreground, publishes via NATS stdin (`--force-stdin` dodges Linux ARG_MAX), authenticates with `natsuser`/`natspassword`, writes one result row per task as soon as it arrives.
- **`retry_run.py`** — re-run an arbitrary subset; same CLI as `run_dataset.py` with explicit `--dataset` / `--results`. Useful for retrying network-failed cases or testing a generator change against a small sample.
- **`merge_retry.py`** — fold a retry result file into a larger one by `instance_id`. Replaces the row in-place; everything else is kept.
- **`stats.py`** — classify a result file's unresolved rows by reason (`run_script_failed`, `setup_script_failed`, `tests_failed_scorer`, `network_git_fetch_failed`, …). Reads `results.jsonl` by default. Useful for quickly bucketing a fresh run.
- **`task.json`** — config skeleton: `run_id`, dummy `task` (the publisher overwrites this per instance), `inference_endpoint`, `harness`. The `run_id` must match what the deployed runner is consuming.

---

## Standalone single-instance debugger (`validate.py`)

For iterating on `install.sh`/`eval.sh` for one instance with live output — without the NATS runner. `validate.py` launches a one-shot disposable docker container against the local `swebench-pro-ubuntu24` image, clones the repo, applies patches, runs `install.sh` + `eval.sh` in sequence, and live-streams the container's stdout+stderr to your terminal. Per instance it asserts:

- **Pre-patch** (no harness): scorer exits **1** (the `fail_to_pass` tests fail, `pass_to_pass` tests pass).
- **Post-patch** (gold harness applied): scorer exits **0** (all tests pass).

```bash
# One instance end-to-end
python ubuntu_24_based_tasks/validate.py --instance-id <iid>

# Pick by index inside a repo (0..78 for qutebrowser, 0..95 for ansible)
python ubuntu_24_based_tasks/validate.py --repo qutebrowser --index 0
python ubuntu_24_based_tasks/validate.py --repo qutebrowser --index 0,5,42

# Random sample (deterministic seed)
python ubuntu_24_based_tasks/validate.py --repo qutebrowser --sample 5

# One per (python_version, date_pin) bucket — slow, exhaustive
python ubuntu_24_based_tasks/validate.py --repo qutebrowser --diverse

# Faster smoke check (skip the gold-patch run)
python ubuntu_24_based_tasks/validate.py --repo qutebrowser --sample 3 --skip-post-patch
```

Note: `validate.py` predates the NATS-runner workflow and does NOT exercise the runner's clone/checkout step (it does its own). It's the right tool for debugging a single instance; the milestone numbers in the [status table](#status) come from the NATS-runner workflow above.

---

## Known flaky failures (Python stdlib drift)

A small fraction of instances may show `unresolved` results not because the model's patch is wrong, but because **Python stdlib behavior drifted between the patch version baked into the upstream `python:X.Y-slim` Docker image and the patch version `uv` installs today**. The upstream Docker base tags (e.g. `python:3.9-slim`) track the *latest* 3.9.x at build time, so even the original eval setup wasn't deterministic across re-runs.

Example: `instance_ansible__ansible-b748edea…` (date_pin 2020-05-20, Python 3.9) — the test `test_prepare_multipart` compares a multipart HTTP body with `\n` line endings, but Python 3.9.5+ ([bpo-43124](https://bugs.python.org/issue43124)) changed `email.generator.BytesGenerator` to emit `\r\n`. Upstream's Docker shipped pre-3.9.5; `uv` installs CPython 3.9.25 today. Same code, same patch, **different test fixture expectation**.

Symptom: 1 test in `fail_to_pass` (or rarely `pass_to_pass`) fails post-patch with a `\n` vs `\r\n` byte-string diff or similar low-level stdlib-behavior diff. Pre-patch run also produces a wrong `pre_rc=0` (because the test passes due to the stdlib bug-fix rather than the model's work).

Not in scope to fix at the toolchain level — would require pinning `uv venv --python X.Y.Z` to a specific patch matched by date_pin, and uv's older patch builds carry their own bug surface. Accepted as ambient noise (~3-5% of Python 3.8 / 3.9 instances).

---

## Limitations

- **Sequential client.** The runner supports parallel consumers (JetStream workqueue retention), but our client publishes one task, waits for one result, then publishes the next. ~75s/instance × 266 ≈ 5.5h wall time. Parallelizing the client would be the single biggest speedup.
- **No incremental output across batches.** `results.jsonl` is rebuilt from per-batch files by hand (or via `merge_retry.py`).
- **Some failures are not actionable from this layer.** The 33 unresolved instances split roughly as: ~10 setup-script gaps where the project's `requirements.txt` resolves to something uv can't build under our constraints; ~15 real test-suite gaps where the gold patch doesn't fully cover the `fail_to_pass` list; ~8 stdlib-drift cases (see above).
- **`FILTERWARNINGS_OVERRIDE_OPT_OUT` is hand-curated** in `generate.py`. When a `pytest.warns`-style test regresses because we suppressed `filterwarnings = error`, the IID gets added by hand.

---

## Lessons learned

Carried over from milestones 1–3 (qutebrowser → ansible → openlibrary):

1. **Probe-by-injection.** When the harness strips stdout/stderr, mutate the eval script in-flight to dump diagnostics (`head .swebench_stdout.log`, `cat .swebench_stderr.log`, `cat output.json`, etc.). Saves hours of guessing.
2. **Group failures by `v<setup_hash>` suffix.** Identical setup hashes mean identical generator output. "13 instances to debug" usually collapses to "2 setup scripts to read."
3. **Reproduce in a clean container before editing the generator.** The runner container is opinionated (no system pip, broken `/etc/localtime`, Node 22, …). Reproduce in `docker exec` against the deployed runner first.
4. **Always run a regression sample.** When changing the generator, include 5–10 previously-resolved instances alongside the newly-affected ones. Zero regressions before claiming a fix.
5. **Commit verified work eagerly.** Smaller commits with "what + why + verification" tell the story; cheap to revert.
6. **No system `python` / `pip` / `pip3`.** The generator must rewrite to `uv pip install` (and strip pip-only flags like `--default-timeout` along the way). Tolerating `make` / `npm` failures via `|| true` lets install.sh proceed to the Python deps the eval actually needs.

### Test ecosystem footguns that keep recurring

- `filterwarnings = error` in `pytest.ini` + any package with a latent `DeprecationWarning` → conftest-import failure → pytest exits 4. Fix: inject `--override-ini="filterwarnings="` into pytest invocations. Hand-curate an opt-out list for tests that rely on `pytest.warns`.
- `uv venv` doesn't include pip by default. Use `--seed` if anything in the project shells out to `python -m pip` (ansible-test does).
- setuptools 69+ imports `jaraco.functools.splat`. Projects pinning `jaraco.functools<4` → `ImportError`. Cap setuptools to `<69`.
- GitHub disabled the `git://` protocol in 2022. Older `.gitmodules` files still pin `git://github.com/…` URLs. Rewrite to HTTPS (`sed` + `git submodule sync` + `git -c url.https://github.com/.insteadOf=git://github.com/ submodule update`) before `git submodule update`.
- Linux `ARG_MAX` limits argv. NATS publish should go via stdin (`--force-stdin` + `docker run -i`).
- `pymarc` / native-build deps fail under build isolation when uv's build-env setuptools doesn't match the project's pinned old jaraco. Re-enable build isolation only for modern `date_pin`s (≥2024-01-01); use `--no-build-isolation` for older ones.
- `babel.localtime` crashes on a broken `/etc/localtime` symlink (some runner images symlink to `/usr/share/zoneinfo//UTC` — note the double slash — which `zoneinfo.ZoneInfo` rejects as "absolute path"). Fix: `export TZ=UTC` in install.sh's env block.
- openlibrary's Node-side build pulls in `iltorb` (a deprecated devDep) that doesn't compile against Node 22. Make `npm install` / `npm ci` and the `make` JS-build targets tolerant (`|| true`); the Python eval doesn't need the JS artifacts.

---

## End-to-end re-do prompt (for Claude Code with Opus or Sonnet)

Use this verbatim to redo the work on a new SWE-bench-style dataset (not necessarily Python). Written so the agent can self-orient and apply the playbook autonomously.

> # Goal
>
> Build a runnable, oracle-validated dataset out of a SWE-bench-style task list. For each task, produce an `install.sh` (sets up the venv + project deps inside a fresh ubuntu:24.04 container) and an `eval.sh` (applies the gold patch, runs the project's tests, scores the result). Wire both through a NATS-based task runner. Surface a final `<lang>_dataset_<flavor>.jsonl` with a per-instance `oracle_check_ok` field (True iff applying the gold patch + running the eval script passes the harness end-to-end).
>
> # Context to orient on first (in order)
>
> 1. `ubuntu_24_based_tasks/README.md` — this file. Read end-to-end before touching anything.
> 2. `ubuntu_24_based_tasks/generate.py` — single source of truth for install.sh + eval.sh generation. Read fully before editing.
> 3. `ubuntu_24_based_tasks/base_image/Dockerfile` — the apt set the runner image ships. New language stacks usually need new deps added here.
> 4. `ubuntu_24_based_tasks/task_runner_testing/run_dataset.py` and `task.json` — the NATS client and config skeleton.
> 5. `ubuntu_24_based_tasks/task_runner_testing/results.jsonl` — canonical companion to the dataset. Format used everywhere.
> 6. Any `.claude/` memory or milestone implementation playbook files committed in the repo.
>
> # Playbook
>
> For each repo / dataset flavor:
>
> 1. **Audit `base_image/Dockerfile`.** Diff the apt deps in the source instance Dockerfiles against the base. Add missing ones (dev headers for native builds, language runtimes, browser drivers). Tell the user to rebuild the runner image; wait for them.
> 2. **Generate** install.sh + eval.sh via `python3 generate.py --repo <repo>` (or `--all-python`). Inspect a few outputs by hand; look for command-translator gaps (`pip3`, `--default-timeout`, `make` targets that abort install).
> 3. **Probe 5 instances** first. Don't run the full set until probes pass; you'll waste hours otherwise. Include at least one instance "most likely to fail" (heaviest install step, most exotic build).
> 4. **Classify failures by symptom** with `python3 stats.py`. Group by `v<setup_hash>` suffix — same hash means same generator output.
> 5. **Use probe-by-injection** for any opaque failure. Mutate the eval script in-flight (search git history for `probe_run4.py` / `probe_ol_exit4.py` for the pattern) to dump stdout/stderr/conftest state.
> 6. **Reproduce in `docker exec` against the runner** before changing the generator. The container's idiosyncrasies (uv-only python, Node 22, broken `/etc/localtime`, etc.) matter.
> 7. **Run a regression sample alongside any generator change**: 5–10 previously-resolved instances + the 5–10 newly-affected ones. Zero regressions before claiming a fix.
> 8. **Commit eagerly** after each verified fix. Small commits with "what + why + verification" tell the story.
> 9. **Final pass**: run the full set, merge per-batch results into `results.jsonl`, annotate `oracle_check_ok` on the dataset, commit.
>
> # Known footguns (carry these in your head from turn 1)
>
> - No system `pip` / `pip3` / `python`. Generator must rewrite to `uv pip install`.
> - `pytest.ini` with `filterwarnings = error` is a trap. Inject `--override-ini="filterwarnings="` into pytest invocations.
> - `uv venv` doesn't ship pip. Use `--seed` if the project shells out to `python -m pip` (ansible-test does).
> - setuptools 69+ needs `jaraco.functools.splat`. Projects pinning `jaraco.functools<4` break. Cap setuptools to `<69`.
> - GitHub killed `git://` in 2022. Rewrite `.gitmodules` to HTTPS before `git submodule update`.
> - Linux ARG_MAX limits argv. NATS publish should go via stdin (`--force-stdin`).
> - `babel.localtime` crashes on broken `/etc/localtime` symlinks. Always `export TZ=UTC`.
> - Native build deps (psycopg2, lxml, pymarc) fail under build isolation when uv's build-env setuptools doesn't match the project's pinned old jaraco. Re-enable build isolation only for modern `date_pin`s (≥2024-01-01); use `--no-build-isolation` for older ones.
> - JS-side build steps (`npm install`, `make js/css/components`) on legacy commits pull in incompatible native addons (iltorb on Node 22). Tolerate via `|| true`; the Python eval doesn't need the JS artifacts.
>
> # What I want at the end
>
> - A regenerated `<lang>_dataset_<flavor>.jsonl` with `oracle_check_ok` annotated on every row.
> - A `task_runner_testing/results.jsonl` that's a 1:1 companion (one row per dataset row, in the same order).
> - Three or four small commits, each with a focused diff: base_image deps + tooling tweaks, generator changes with reproduction details, final annotation + per-batch artefacts, optional final cleanup.
> - Confirmation that the regression sample stayed green.
> - An honest "still unresolved" list at the end. Don't paper over real test failures.
>
> # Constraints
>
> - Use TodoWrite from the start. Long autonomous tasks should always have a visible todo list.
> - Never invent file paths or instance IDs — read the dataset first.
> - Don't emit `pip install` / `python -m pip` from install scripts even when "it would be simpler". The runner image enforces the no-system-python rule for a reason.
> - Don't fork generator logic into separate scripts. `generate.py` is the single source of truth.
> - When in doubt about whether a fix regresses something, run a regression sample. The `advisor()` tool is worth calling once before committing to a non-trivial generator change.
