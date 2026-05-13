Meta-lessons from milestone 2
Diagnostic methodology
Probe-by-injection is the right diagnostic tool. When the harness/runner strips stdout/stderr, the fastest way to see what's really happening is to mutate the eval_script in-flight to dump diagnostics (the probe_*.py pattern). Always do this before hypothesizing; one probe replaced 30 minutes of guessing about pytest exit 4.
Group failures by hash, not by symptom. Instance IDs encoded v<setup_hash> — all 6 setup_exit_127 shared one setup hash, all 7 setup_exit_2 shared another. Recognizing this collapsed "13 instances to debug" into "2 setup scripts to read."
Reproduce in a clean container before editing the generator. The repro shell scripts I built (repro_distutils.sh, etc.) caught the actual failing step, not the line that printed the final error. Without that, I'd have fixed the wrong thing.
Generator-edit discipline
Always backup the dataset before regenerating. python_dataset_ubuntu24.before_qute_fixes.jsonl saved us from needing to re-derive state if a generator change went sideways.
Always run a regression sample alongside the target fix. The qutebrowser fixes looked clean until you pointed out my regression sample was qutebrowser-only — ansible was untested. Now baked-in: when changing the generator, sample across all repos in scope.
Verify the transform's regex against real source files. My inject_filterwarnings_override matched pytest inside echo "# pytest would be preferred..." until I tightened it to anchor on line start. Cheap cosmetic bugs become real bugs when a future grep depends on them.
Watch for "passes echo strings" → trust the empirical test diff. Diffing the regenerated output against committed output catches transforms that subtly hit unintended code paths.
Approach calibration
Default to the conservative scope of a fix, then widen. The advisor recommended narrowing --no-build-isolation to date_pin < 2024-01-01 instead of capping setuptools. Followed that, was right.
One bug at a time. I tried to commit Fix 1+2+3 together initially. Splitting them by symptom (and verifying each independently with a probe) made each commit message clear and each revert (if needed) cheap.
The advisor sees what I miss. Two times: catching that my regression sample was qute-only, and recommending the build-isolation approach over setuptools-capping. When stakes are >5 commits or >100 lines, call it.
Process
Commit verified work eagerly. You prompted me to commit after each verified milestone, not at the end. Smaller commits, cleaner story, no "lost work if I'm interrupted."
Don't let git add go wild. Twice I almost staged 91 openlibrary out/ dirs from milestone-3 territory. Filter explicitly.
Background long-running runs; never block on them. The retry pipeline + run_in_background=true + completion notifications kept the foreground productive.
Test ecosystem insights (might recur in milestone 3)
filterwarnings = error in pytest.ini is a trap that breaks under modern Python. Projects pinning old deps in a CI hardened against deprecations all share this fragility.
--seed is venv hygiene, not just an ansible-test workaround. Any project that internally shells out to python -m pip needs it. Worth assuming yes by default.
setuptools 69+ has a transitive dep (jaraco.functools.splat) that breaks when projects pin old jaraco. A persistent footgun for projects with stable old requirements pins.
pytest.warns-based tests fail under broad filterwarnings resets. Hand-curated opt-out list works for small N; if it grows, switch to grepping test files at base_commit for pytest.warns(.