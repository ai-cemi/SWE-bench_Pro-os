# task_runner_testing

Scripts and recorded results for running the SWE-bench Pro Python dataset
through the itf-demo NATS task runner with the **oracle harness** (apply
the gold patch, run the eval script, see if it passes). Used to set the
`oracle_check_ok` field on `python_dataset_ubuntu24.jsonl`.

---

## 1. Current status

Last full sweep over the 266 instances in `python_dataset_ubuntu24.jsonl`:

| Repo        | Resolved | %     |
|-------------|---------:|------:|
| ansible     |   83/96  | 86.5% |
| qutebrowser |   66/79  | 83.5% |
| openlibrary |   84/91  | 92.3% |
| **Total**   | **233/266** | **87.6%** |

`oracle_check_ok=True` in the dataset iff the harness reported
`resolved: True` AND there was no harness/transport error for that
instance.

---

## 2. Result files

All paths relative to `task_runner_testing/`.

| File | Rows | What's in it |
|---|---|---|
| `results.jsonl` | 266 | Canonical merged result file, **in dataset order**. One row per `instance_id`, holding the full harness payload (`resolved`, `error`, `raw.evaluation`, timestamps). Companion to `../python_dataset_ubuntu24.jsonl`. |
| `results.qute_ansible.jsonl` | 175 | Raw per-batch run output for the qutebrowser + ansible slice. |
| `results.openlibrary.jsonl` | 91 | Raw per-batch run output for the openlibrary slice. |
| `qute_ansible.jsonl` | 175 | Dataset subset fed to the runner for batch 1 (qute + ansible filter of `python_dataset_ubuntu24.jsonl`). |
| `openlibrary.jsonl` | 91 | Dataset subset fed to the runner for batch 2 (openlibrary filter). |

Schema of every result row (canonical):

```json
{
  "instance_id": "instance_<org>__<repo>-<commit>-v<setup-hash>",
  "resolved": true,                      // null if error=true
  "error": false,                        // true if harness never replied
  "raw": {                               // null if error=true
    "run_id": "...",
    "task_id": "<instance_id>",
    "success": true,                     // false on harness-side errors
    "error": "setup script failed (exit 2): ...",   // present on setup failure
    "evaluation": {                      // present iff success=true
      "resolved": true,
      "eval_script_results": [
        {"name": "0", "passed": true, "output": "eval.sh: ..."}
      ],
      "total_scripts_run": 1
    },
    "started_at": "2026-05-13T08:00:00Z",
    "finished_at": "2026-05-13T08:01:11Z"
  }
}
```

---

## 3. Reproducing the work

### The base image — `base_image/Dockerfile`

`base_image/Dockerfile` is the **single edit point for the apt deps our
install scripts need**. It's a focused ~100-line dependency list: the union
of system packages our install scripts depend on (libpq-dev for psycopg2
source builds, chromium-driver for selenium tests, the Qt/X11 stack for
qutebrowser, libxml2-dev/libxslt1-dev for lxml, etc.). When you add a
new repo to the dataset, audit its instance Dockerfiles' apt deps
against this file and add what's missing.

The **runner image that actually runs in production** lives in itf-demo at
`crates/workload_agentic_coding_runner/Dockerfile`. It's a two-stage build
(stage 1 compiles the rust `task_runner` binary, stage 2 lays down the
apt set + uv + the runner binary). Our `base_image/Dockerfile` is
decoupled from that build plumbing on purpose: easier to review, easier
to diff when the only thing changing is the apt set.

**Workflow when apt deps change:**
1. Edit `base_image/Dockerfile` in this repo (the focused diff).
2. Manually mirror the apt section into
   `itf-demo/crates/workload_agentic_coding_runner/Dockerfile`.
3. Rebuild + restart the runner.

### Prereqs

- `itf-demo` task runner running on docker-compose. NATS on host port 4222
  with auth `natsuser`/`natspassword`. Runner image must include the apt
  set from `base_image/Dockerfile` (libpq-dev, chromium-driver, the Qt/X11
  stack for qutebrowser, etc.).
- Local `docker` CLI (the scripts shell out to `natsio/nats-box:latest`
  for sub/pub).
- Python 3.10+ on the host (just stdlib).

### Generate dataset + scripts

```bash
cd ../  # ubuntu_24_based_tasks/
python3 generate.py --all-python
```

This rebuilds `python_dataset_ubuntu24.jsonl` (266 records) and the
per-instance `out/<instance_id>/{install.sh,eval.sh}` pairs (gitignored,
regenerated on demand).

### Build the batch subsets

```bash
python3 -c "
import json
qa = open('task_runner_testing/qute_ansible.jsonl', 'w')
ol = open('task_runner_testing/openlibrary.jsonl', 'w')
for line in open('python_dataset_ubuntu24.jsonl'):
    iid = json.loads(line)['instance_id']
    if iid.startswith(('instance_ansible__','instance_qutebrowser__')):
        qa.write(line)
    elif iid.startswith('instance_internetarchive__'):
        ol.write(line)
"
```

### Run

```bash
cd task_runner_testing/
# Batch 1: qutebrowser + ansible (~2h)
python3 run_dataset.py --dataset qute_ansible.jsonl \
    --results results.qute_ansible.jsonl --timeout 1200

# Batch 2: openlibrary (~1.5h; runs sequentially, can run in parallel
# with batch 1 if the runner supports >1 consumer)
python3 run_dataset.py --dataset openlibrary.jsonl \
    --results results.openlibrary.jsonl --timeout 1200
```

Each row is appended as it completes, so resuming on crash is trivial:
re-run with `--start-from N`.

### Merge + annotate

```bash
# Backup the dataset first
cp ../python_dataset_ubuntu24.jsonl ../python_dataset_ubuntu24.jsonl.bak

# Merge per-batch result files into one results.jsonl in dataset order
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

# Annotate oracle_check_ok
python3 -c "
import json
res = {json.loads(l)['instance_id']: json.loads(l) for l in open('results.jsonl')}
def ok(r):
    if r is None or r.get('error'): return False
    raw = r.get('raw') or {}
    if raw.get('error'): return False
    return bool(r.get('resolved'))
import shutil, os
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

### Useful tooling

- **`stats.py`** — classify a result file's unresolved rows by reason
  (`run_script_failed`, `setup_script_failed`, `tests_failed_scorer`,
  `network_git_fetch_failed`, …). Reads `results.jsonl` by default.
  ```bash
  python3 stats.py
  ```

- **`retry_run.py`** — re-run an arbitrary subset; useful for retrying
  network-failed cases or testing a generator change against a small
  sample.
  ```bash
  python3 retry_run.py --dataset some_subset.jsonl \
      --results results.retry.jsonl --timeout 900
  ```

- **`merge_retry.py`** — fold a retry result file into a larger one
  by `instance_id`. Replaces the row in-place; everything else is kept.
  ```bash
  # Default: merge results.retry.jsonl into results.all.jsonl
  python3 merge_retry.py
  ```

- **`run_dataset.py`** — the canonical runner. Sequential, foreground,
  publishes via NATS stdin (`--force-stdin` dodges Linux ARG_MAX),
  authenticates with `natsuser`/`natspassword`, writes one result row
  per task as soon as it arrives.

- **`task.json`** — config skeleton: `run_id`, dummy `task` (the
  publisher overwrites this per instance), `inference_endpoint`,
  `harness`. The `run_id` must match what the runner is consuming.

---

## 4. Limitations and lessons learned

### Limitations

- **Sequential runs.** The runner supports parallel consumers
  (JetStream workqueue retention), but our client publishes one task,
  waits for one result, then publishes the next. ~75s/instance ×
  266 = ~5.5h wall time. Parallelizing the client would be the single
  biggest speedup.
- **No incremental output.** `results.jsonl` is rebuilt from per-batch
  files by hand. If you change one instance and re-run, you have to
  merge manually (or use `merge_retry.py`).
- **Some failures are not actionable from this layer.** The 33
  unresolved instances split roughly as:
  - ~5 setup-script gaps we haven't probed yet, 
  - ~10 real test-suite gaps where the gold patch doesn't fully cover the `fail_to_pass` list,
  - ~5 unfixed `tests_failed_scorer` cases that need per-instance investigation.
- **No per-instance opt-out for the filterwarnings override** beyond
  the hand-curated list in `generate.py:FILTERWARNINGS_OVERRIDE_OPT_OUT`.
  When a `pytest.warns`-style test regresses, you add the IID by hand.

### Lessons learned (carried over from milestones 1–3)

1. **Probe-by-injection.** When the harness strips stdout/stderr,
   mutate the eval script in-flight to dump diagnostics. Saves hours
   of guessing.
2. **Group failures by `v<setup_hash>` suffix.** Identical setup hashes
   mean identical generator output. "13 instances to debug" usually
   collapses to "2 setup scripts to read."
3. **Reproduce in a clean container before editing the generator.**
   The runner container is opinionated (no system pip, broken
   `/etc/localtime`, Node 22, …). Reproduce in `docker exec` first.
4. **Always run a regression sample.** When changing the generator,
   include 5–10 previously-resolved instances in the rerun to catch
   regressions.
5. **Commit verified work eagerly.** Smaller commits, cleaner story.
6. **Watch for runner image vs. install-script confusion.** "No
   system python/pip" is a hard rule; the generator must never emit
   `pip install` / `pip3 install` / `python -m pip` — only `uv pip
   install`. Tolerating `make`/`npm` failures lets install.sh proceed
   to the Python deps the eval actually needs.
7. **Test ecosystem footguns that keep recurring**
   - `filterwarnings = error` in `pytest.ini` + any package with a
     latent `DeprecationWarning` → conftest-import failure → pytest
     exit 4.
   - `uv venv` doesn't include pip by default. Use `--seed` if anything
     in the project shells out to `python -m pip` (ansible-test does).
   - setuptools 69+ imports `jaraco.functools.splat`. If a project
     pins `jaraco.functools<4`, setuptools breaks at runtime. Cap
     setuptools to `<69`.
   - GitHub disabled the `git://` protocol in 2022. Older
     `.gitmodules` files still pin `git://github.com/...` URLs.
     Always rewrite to HTTPS before `git submodule update`.

---

## 5. End-to-end re-do prompt (for Claude Code with Opus or Sonnet)

Use this prompt verbatim to redo the work of milestones 1–3 on a new
SWE-bench-style dataset (not necessarily Python). It's written so the
agent can self-orient and apply the playbook autonomously.

> # Goal
>
> Build a runnable, oracle-validated dataset out of a SWE-bench-style
> task list. For each task, produce an `install.sh` (sets up the venv
> + project deps inside a fresh ubuntu:24.04 container) and an `eval.sh`
> (applies the gold patch, runs the project's tests, scores the result).
> Wire both through a NATS-based task runner. Surface a final
> `python_dataset_<flavor>.jsonl` with a per-instance `oracle_check_ok`
> field (True iff applying the gold patch + running the eval script
> passes the harness end-to-end).
>
> # Context you should orient on first (in order)
>
> 1. `ubuntu_24_based_tasks/generate.py` — single source of truth for
>    install.sh + eval.sh generation. Read it end-to-end before
>    editing anything.
> 2. `ubuntu_24_based_tasks/base_image/Dockerfile` — the apt set the
>    runner image ships. New language stacks usually need new deps
>    added here.
> 3. `ubuntu_24_based_tasks/task_runner_testing/run_dataset.py` and
>    `task.json` — the NATS client and config skeleton.
> 4. `ubuntu_24_based_tasks/task_runner_testing/results.jsonl` —
>    canonical companion to the dataset. Format used everywhere.
> 5. The .claude/ memory and any milestone implementation playbook
>    files committed in the repo (search for them).
>
> # Playbook (the workflow I want you to follow)
>
> For each repo / dataset flavor:
>
> 1. **Audit `base_image/Dockerfile`.** Diff the apt deps in the source
>    instance Dockerfiles against the base. Add missing ones
>    (dev headers for native builds, language runtimes, browser
>    drivers). Tell the user to rebuild the runner image; wait for
>    them.
> 2. **Generate** install.sh + eval.sh via
>    `python3 generate.py --repo <repo>`. Inspect a few outputs by
>    hand; look for command-translator gaps (e.g. `pip3`, weird flags,
>    `make` targets that abort install).
> 3. **Probe 5 instances** first. Don't run the full set until probes
>    pass; you'll waste hours otherwise. Include at least one instance
>    "most likely to fail" (heaviest install step, most exotic build).
> 4. **Classify failures by symptom** with
>    `python3 stats.py`. Group by `v<setup_hash>` suffix — same hash
>    means same generator output.
> 5. **Use probe-by-injection** for any opaque failure. Mutate the
>    eval script in-flight (`probe_*.py` pattern shown in earlier
>    commits — search git history for `probe_run4.py`) to dump
>    stdout/stderr/conftest state.
> 6. **Reproduce in `docker exec` against the runner** before changing
>    the generator. The container's idiosyncrasies (uv-only python,
>    Node 22, broken /etc/localtime, etc.) matter.
> 7. **Run a regression sample alongside any generator change**: 5–10
>    previously-resolved instances + the 5–10 newly-affected ones.
>    Zero regressions before claiming a fix.
> 8. **Commit eagerly** after each verified fix. Small commits with
>    "what + why + verification" tell the story.
> 9. **Final pass**: run the full set, merge per-batch results into
>    `results.jsonl`, annotate `oracle_check_ok` on the dataset,
>    commit.
>
> # Known footguns (carry these in your head from turn 1)
>
> - No system `pip`/`pip3`/`python`. Generator must rewrite to `uv pip`.
> - `pytest.ini` with `filterwarnings = error` is a trap. Inject
>   `--override-ini="filterwarnings="` into pytest invocations.
> - `uv venv` doesn't ship pip. Use `--seed` if the project shells out
>   to `python -m pip` (ansible-test does).
> - setuptools 69+ needs jaraco.functools.splat — projects pinning
>   `jaraco.functools<4` break. Cap setuptools to `<69`.
> - GitHub killed `git://` in 2022. Rewrite `.gitmodules` to HTTPS
>   before `git submodule update`.
> - Linux ARG_MAX limits argv. NATS publish should go via stdin
>   (`--force-stdin`).
> - `pymarc`/native-build deps fail under build isolation when uv's
>   build-env setuptools doesn't match the project's pinned old
>   jaraco. Re-enable build isolation only for modern `date_pin`s
>   (>=2024-01-01); use `--no-build-isolation` for older ones.
>
> # What I want at the end
>
> - A regenerated `python_dataset_<flavor>.jsonl` with
>   `oracle_check_ok` annotated on every row.
> - A `task_runner_testing/results.jsonl` that's a 1:1 companion
>   (one row per dataset row, in the same order).
> - Three small commits: (a) base_image deps + .gitignore + tooling
>   tweaks, (b) generator changes with reproduction details, (c)
>   final annotation + per-batch artefacts.
> - Confirmation that the regression sample stayed green.
> - An honest "still unresolved" list at the end. Don't paper over
>   real test failures.
>
> # Constraints
>
> - Use TodoWrite from the start. Long autonomous tasks should always
>   have a visible todo list.
> - Never invent file paths or instance IDs — read the dataset first.
> - Don't add `pip install` / `python -m pip` to install scripts even
>   when "it would be simpler". The runner image enforces the no-
>   system-python rule for a reason.
> - Don't fork generator logic into separate scripts. `generate.py`
>   is the single source of truth.
> - When in doubt about whether a fix regresses something, run a
>   regression sample. The advisor agent (`advisor()` tool) is worth
>   calling once before committing to a non-trivial generator change.
