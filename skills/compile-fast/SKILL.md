---
name: compile-fast
description: Verify Java source changes using the Python compile-fast engine and real javac, with module incremental caches and structured diagnostics. Use after editing Java code or when asked for fast compilation validation in Maven projects. Does not replace tests or the full build gate.
---

# Compile Fast

Run `python <this-skill-directory>/scripts/run.py <command> ...`. The installer bundles the engine, so no pip packages are required. The examples below use `compile-fast` as shorthand for that invocation; the installed console command or `python -m compile_fast` from the engine repository also works. The engine itself is Python; the target workspace requires a JDK.

## Context and verification

1. Locate the target project's root and `.compile-fast/model.json`.
2. For Maven, run `compile-fast resolve-maven --project <root>` when the model is missing or build files changed. Preserve the user's Maven profiles/properties with `--maven-arg=-Pprofile` / `--maven-arg=-Dkey=value`. Maven may access dependency repositories. Resolution never installs or builds the target project automatically.
3. Run `compile-fast verify --project <root> --agent <agent-id>`. Git mode includes staged, unstaged, untracked and deleted files. Use `--base <revision>` to include committed changes against another revision. For a non-Git workspace or an explicit agent patch, supply `--changed <relative-path> ...`; include deleted paths too. `--changed` alone verifies all modeled main sources without Git detection.
4. Read stdout as JSON even when the exit code is nonzero. State the actual level, scope and cache evidence in the response.

Exit codes: `0=PASS`, `1=FAIL`, `2=UNKNOWN`, `3=INFRA_ERROR`.

- `PASS`: only the reported L2 main-source scope passed. It does not prove tests, runtime behavior or the full build gate.
- `FAIL`: use compiler file/line/column diagnostics to fix relevant source. Retry with current files; stop and report after three attempts with the same compiler error. Do not invent candidates or change unrelated code to suppress a failure.
- `UNKNOWN`: repair the context or run the project's normal build/test command. Do not change `contextComplete` merely to force verification. Missing dependencies, generators, annotation processors, JPMS and changed test source sets need escalation.
- `INFRA_ERROR`: inspect missing tools, timeouts or filesystem errors before changing source.

The engine recompiles entire changed modules and downstream modules. ABI fingerprints now control downstream cache invalidation; optional OpenRewrite supplies attributed LST references. Candidate reference graphs still require conservative module fallback. Do not promise file-level precision or unmeasured speedups. `.compile-fast/results/<verificationId>.json` contains the evidence; stale-workspace results are not current diagnoses.

For manually supplied or Gradle compilation contexts, read [model.md](references/model.md). Do not infer a complete classpath from imports. Preserve the existing user-authorized build/test workflow for L3.

## Extended verification

Use `--level auto` for L1/L2; add `--allow-build` when the user has authorized the project's build/test commands to execute L3 as well. `--level L3 --allow-build` runs the normal gate directly. Read `.compile-fast/gates.json` before custom gates. Respect `repairPolicy.action=STOP_AND_REVIEW`.

For Gradle use `resolve-gradle`. `index`, `impact`, and `abi --path <class-directory-or-jar>` expose indexes and changes. Optional syntax dependencies: `pip install "compile-fast[semantic]"`; OpenRewrite additionally needs JPype1 and `setup-rewrite`, then `rewrite-index`. Do not claim complete type attribution when that backend is unavailable. L2 and L3 remain the authorities for compiler/build failures.

For queued multi-agent workflows, `serve --workspace id=path` exposes authenticated HTTP verification endpoints. Add `--allow-builds` only for authorized build execution. Send registered workspace IDs, not arbitrary paths. New submissions supersede older jobs for the same workspace; separate worktrees preserve independent agents' revisions. The token path is printed at startup; do not include the token in reports.
