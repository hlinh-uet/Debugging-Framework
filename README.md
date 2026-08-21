# Debugging Framework

The framework automates **Fault Localization (FL)** and **Automated Program Repair
(APR)** for C/C++ projects. Codex edits the caller-supplied Git project directly;
the framework obtains the Git diff from the filesystem, resets the baseline, applies
and tests the diff before returning the result, and then restores the original branch/HEAD.

CodeGraph 1.5.0 is bundled to help Codex navigate the source code.
Users do not need to install Node.js, npm, npx, or CodeGraph separately.

## Requirements

- Python 3.10 or later.
- Git.
- Codex CLI installed and authenticated with `codex login`.
- A C/C++ project build/test environment:
  - `host`: the current machine has the compiler, dependencies, and test tools; or
  - `image`: a prepared Docker/Podman image.

The framework does not install dependencies, build/pull images, or switch
automatically between `host` and `image`.

On macOS with Docker Desktop, validation copies the project into a temporary
Linux filesystem inside the container before running commands, then synchronizes
the artifacts back. This preserves POSIX permission semantics for tests using
`access(2)`/`stat(2)`; it is an image-backend detail and does not create another
source workspace on the host.

## Installation

### Install the wheel for a partner

```bash
pipx install /path/to/debugging_framework-0.7.0-py3-none-any.whl
codex login
debugging-framework --help
```

The wheel includes CodeGraph for macOS ARM64 and Linux x64. On other platforms,
`DEBUGGING_CONTEXT_MODE=auto` skips CodeGraph and preserves the Codex FL/APR flow.

CodeGraph is zero-config by default: the framework selects the bundled runtime,
creates a non-interactive index in the project baseline, verifies the index, and
makes the `codegraph` command available to the Codex session. The framework does
not install Git hooks or daemons, or change global CodeGraph configuration on the
partner's machine.

### Install from source for development

```bash
cd /path/to/Debugging-Framework
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
codex login
```

## Run a repair step by step

### Step 1: initialize the project

For the `host` environment:

```bash
cd /path/to/project
debugging-framework init \
  --environment host \
  --test-id '<test-id>'
```

Example using a prepared image:

```bash
debugging-framework init \
  --environment image \
  --environment-image project-tests:prepared \
  --test-id '<test-id>'
```

This command detects the current workflow to create a **draft contract**, and also
creates the directory for the failure log:

```text
.debugging-framework.json
.debugging-framework/
```

The partner should review `setup`, `build`, and especially `regression_test` before
running a repair, then commit the contract and `.gitignore` to keep the working tree
clean. The failure log in `.debugging-framework/` may remain ignored. Example CMake/CTest contract:

```json
{
  "schema_version": 6,
  "system": "cmake",
  "setup": [
    ["cmake", "-S", ".", "-B", ".debugging-framework/build", "-DBUILD_TESTING=ON"]
  ],
  "build": [
    ["cmake", "--build", ".debugging-framework/build", "--parallel", "4"]
  ],
  "regression_test": [
    ["ctest", "--test-dir", ".debugging-framework/build", "--output-on-failure"]
  ],
  "repair": {
    "failing_tests": ["<test-id>"],
    "attempts": 2
  },
  "environment": {
    "mode": "host"
  }
}
```

### Step 2: save the actual failure output

Run the project's failing test and save its complete output to:

```text
.debugging-framework/failure.log
```

This file must not be empty. The framework trusts the caller-supplied failure log
and failing test IDs, uses them as the primary failure evidence for CodeGraph/Codex,
and does not run tests on the original source before editing begins.

### Step 3: check the environment

```bash
debugging-framework doctor . --config .debugging-framework.json
```

Check any `[FAIL]` lines before running the repair. With CodeGraph mode `auto`,
a `[WARN]` status only means Codex will use ordinary search/read operations.

### Step 4: run FL + APR

```bash
debugging-framework repair \
  --project . \
  --config .debugging-framework.json \
  --failure-output .debugging-framework/failure.log \
  --output /tmp/fix.patch
```

The framework will:

1. Accept the caller-supplied failure log and failing test IDs without running the original source.
2. Lock the caller-supplied clean Git project, record branch/HEAD recovery metadata, and prepare CodeGraph when available.
3. Reset to the baseline before each attempt; have Codex perform FL and APR directly in the project using the supplied failure log.
4. Extract the canonical Git diff from the actual changes, reset, and apply that diff for setup/build/test.
5. If `target_test` is configured, run it first; a target failure is passed as feedback to the next attempt.
6. Run the full `regression_test` suite only after the target passes.
7. If no `target_test` is configured, run the full suite directly for each attempt.
8. Write the selected patch to `/tmp/fix.patch`.
9. Restore the original branch/HEAD and clean up build/runtime artifacts created by the run.

The framework does not copy the source tree. The partner's Git project must be clean
and contain no ignored artifacts except `.debugging-framework/`. If the caller
provides a source export without Git, the config must explicitly confirm that it is
disposable data:

```json
{
  "workspace": {
    "disposable": true,
    "initialize_git_if_missing": true
  }
}
```

The framework then creates a temporary Git baseline in the source export and removes
that metadata after restoring the project. Benchmark adapters such as Defects4C use
this mode. During a run, recovery metadata is stored at
`<results>/<project>/workspace-recovery.json`; if the process/SSH connection is
interrupted, the next repair keeps the same project lock and restores the baseline
before touching artifacts from the new run.

### Step 5: review the results

The complete results are stored by default at:

```text
~/.local/state/debugging-framework/results/<project-name>/
```

The files you will usually inspect are:

- `patch.diff`: the selected patch.
- `result.json`: the final validation status.
- `attempts/attempt_NN/`: the prompt, Codex response, canonical workspace patch, and logs.

Codex returns only a repair description and a list of paths; it does not need to
copy the diff into JSON. The framework adds the canonical workspace diff to
`repair.diff` in `result.json` and uses the same content for validation and
`patch.diff`.

The failure baseline comes only from the caller, so it does not generate artifacts
or run commands. Each repair result directory contains only `attempts/`,
`patch.diff` (when Codex creates a candidate), and `result.json`.

`status=plausible` always requires the full regression suite to pass. If the contract
has `target_test`, the target must also pass first. Other statuses such as
`cleanfix`, `noisefix`, `nonefix`, `negfix`, or `invalid` should be reviewed in
`result.json`.

## Common configuration

Most job configuration should be placed in `.debugging-framework.json`:

```json
{
  "schema_version": 6,
  "system": "partner-contract",
  "setup": [
    ["./scripts/configure.sh"]
  ],
  "build": [
    ["./scripts/build.sh"]
  ],
  "target_test": [
    {
      "command": ["./scripts/run_tests.sh", "--case", "{test_id}"],
      "evidence_pattern": "[1-9][0-9]* tests? (passed|failed)",
      "failure_pattern": "[1-9][0-9]* tests? failed"
    }
  ],
  "regression_test": [
    {
      "command": ["./scripts/run_tests.sh", "--all"],
      "evidence_pattern": "[1-9][0-9]* tests? (passed|failed)",
      "failure_pattern": "[1-9][0-9]* tests? failed"
    }
  ],
  "repair": {
    "failing_tests": ["<test-id>"],
    "attempts": 2,
    "model": "gpt-5.6-sol",
    "codex_timeout": 1800,
    "command_timeout": 1800,
    "jobs": 4,
    "source_extensions": [".re"]
  },
  "environment": {
    "mode": "host"
  }
}
```

Useful environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DEBUGGING_CODEX_BIN` | `codex` | Codex CLI path |
| `DEBUGGING_CODEX_MODEL` | `gpt-5.6-sol` | Model used for repair |
| `DEBUGGING_RESULTS_DIR` | user state directory | Results location |
| `DEBUGGING_CONTEXT_MODE` | `auto` | CodeGraph mode: `auto`, `required`, `off` |
| `DEBUGGING_CODEGRAPH_TIMEOUT` | `3600` | Graph preparation timeout in seconds |

You do not need to configure the CodeGraph executable when using the bundled
wheel/repository. For normal workflows, no CodeGraph variables are required; the
variables in the table are advanced overrides only.

### CodeGraph modes

- `auto` — recommended: use CodeGraph when ready and fall back automatically if it fails.
- `required` — stop the job if CodeGraph is unavailable; useful for benchmarks/A-B tests.
- `off` — disable CodeGraph and use the original Codex flow.

CodeGraph supports repository navigation only. Failure evidence, the output schema,
FL/APR logic, and patch validation remain controlled by the current framework.

## Partner build/test contract

In the `repair` workflow, `regression_test` is required and must run the project's
official full suite. `setup` and `build` may be empty lists when the image/host has
the corresponding artifacts prepared. `target_test` is optional and is only for
fast failure; it does not replace full-suite validation.

The framework can detect CMake, Meson, Make/Autotools, Bazel, and Ninja to help
`init` generate a draft. Auto-detection does not replace the contract approved by
the partner. Inspect the contract before running:

```bash
debugging-framework inspect /path/to/project --environment host
```

If `target_test` is not declared, the framework runs `regression_test` directly
exactly once. The framework does not add `ctest -R`, pass test IDs to unknown
runners, or pretend that the full suite is a target. `{test_id}` is valid only in
`target_test`; a `regression_test` containing this placeholder is rejected.

Each custom test command needs `evidence_pattern` to prove that at least one test
actually ran. Add `failure_pattern` if the runner's failure output does not use a
common format. Commands run directly without shell expansion.

## Other commands

```bash
# Show the build/test plan without running commands
debugging-framework inspect /path/to/project --environment host

# Check Codex, CodeGraph, and the environment
debugging-framework doctor /path/to/project --environment host

# Validate an existing patch
debugging-framework validate /path/to/project /tmp/fix.patch --environment host
```

## For maintainers

```bash
# Build the wheel for delivery to a partner
python -m pip install build
python -m build --wheel
```

The wheel is approximately 115 MB because it contains two CodeGraph runtimes.
CodeGraph metadata and licenses are in `third_party/codegraph/`.

The framework allows automatic repair to change only production C/C++ source and
header files. Patches that modify tests, fixtures, or build/test infrastructure are
rejected by validation.
