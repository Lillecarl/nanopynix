# Useful commands

A bare `pytest` runs every suite: `testpaths` in `pytest.ini` names each
project's own. `pytest tests` now selects the self-checks and the static
gates alone, because issue #130 moved each suite beside its project.

A plain `pytest` invocation uses `--nix-test-backends local` only. This backend
runs in-process and uses one store. CI tests the daemon backend separately, in
the `test-daemon-*` matrix jobs and in the TSan workflows. Those jobs pass
`--nix-test-backends local,daemon`. Pass `--nix-test-backends local,daemon`
yourself to reproduce a daemon failure on your machine.

- direnv exec . timeout 500 pytest
- direnv exec . timeout 500 pytest --cov --cov-report=term-missing --cov-report= # coverage report. It includes the Nix worker subprocess that multiprocessing starts with the forkserver. See the `sitecustomize.py` of `nanopynix_testing`, which that project's `beartype_hook` puts on PYTHONPATH.

The coverage table has approximately 88 lines, so agent mode does not print the
table to the terminal. The terminal shows `[pytest-agent] coverage: NN%` for
the total. Agent mode writes the table to
`.pytest-agent/runs-NNNN/reports.txt`, and the line above the total gives this
path.

Read that file for the row of each file and for the `Missing` columns. Do not
make a conclusion about coverage from the terminal alone. Do not run the tests
again with a different reporter to get the table. To print the table to the
terminal, pass `--agent-max-summary-lines=0`.

- direnv exec . pyright
- direnv exec . ruff check --fix
- direnv exec . ruff check --config ruff-strict.toml --fix # This configuration reports zero findings now. Keep it at zero. A new finding comes from your change.

Never run either ruff configuration with `--unsafe-fixes`. The strict
configuration disables TC001-003, because these rules break type-checking at
runtime. `ruff-strict.toml` gives the reason and the measurement behind it.
`--unsafe-fixes` is the mechanism that applies the same damage in bulk to
another rule.

CI runs those three commands, `ruff format --check`, `shellcheck` over
`scripts/`, and the suite of each subproject that the repository run does not
reach, in the `static-checks` job. Each one is a derivation in
`nix/checks.nix`, and each is a package. Build them to run a gate the way CI
runs it, in a sandbox and not in the dev shell:

- nix build --file . --no-link --keep-going checks.lint checks.lint-strict checks.format checks.types checks.shell checks.grpclib-transports checks.pytest-agent checks.test-support checks.nanopynix-helpers checks.nix-daemon-protocol checks.pynixd

Do not use `nix flake check` for this. That command evaluates every package,
and `packages.shell` cannot evaluate in a pure flake evaluation.

**Build with `--file .` and an attribute path, and not with `.#`.** Every
attribute of `default.nix` is reachable that way, at any depth, so a nested
attribute needs no flat name in `flake.packages`. `flake.packages` holds the
finished products only. `FLAKE_COMPATISH_DISABLE_OVERRIDES=1` makes a
`--file .` evaluation agree with a flake evaluation: it stops `nix/compat.nix`
overriding `self` with the local checkout, and reads the lockfile instead.
Every CI workflow sets it, so CI and a local run build the same derivation.

- nix build --file . pkgs.nixVersions.nix_2_34.src --no-link --print-out-paths # Download the source code of a Nix package and print the path to it.

# GitHub Actions

**`.github/workflows/*.yml` is generated. Do not edit it.** `ci/render.py`
writes each file from `ci/workflows/on_*.nix`. Change the Nix source, then run
`direnv exec . python ci/render.py` and commit both. A hand edit passes every
static gate and then fails `test_checked_in_workflows_are_current` in each job
of the test matrix, which costs a whole CI run to learn.

**A `run:` body of a step is one line, and it holds no `${{ ... }}`
expression.** `tests/meta/test_ci_step_policy.py` enforces both rules, and it
names the workflow, the job and the step when one breaks.

Put the body in `ci/steps.nix`, which builds each one as a
`writeShellApplication`, and call it from the step:

```yaml
env:
  CI_STEP: ciSteps.nix_2_34-tsan
run: nix build --file . "$CI_STEP" --out-link result --print-build-logs
run: ./result/bin/nanopynix-ci soak-seeds
```

Three reasons apply, and each one names a thing that a body in a YAML file
cannot do:

- **A gate reads a script.** `writeShellApplication` runs `shellcheck` over
  what it builds. `check-shell` reads `scripts/*.sh`, and a bash string inside
  a Nix file is not a script to any tool.
- **A script runs here, and against one Nix version.** These are the commands
  that CI runs:
  - `nix run --file . ciSteps.commit-subjects`
  - `nix build --file . ciSteps.nix_2_34 --out-link result`, and then
    `BACKEND=local ./result/bin/nanopynix-ci soak`
- **`runtimeInputs` names each tool.** A step that declares `jq`, `git` and
  `unshare` does not depend on the tools of the runner image, so the step also
  runs on a different CI service.

Give a value to a step through `env:`, at the step or at the job. GitHub puts
an expression into the text before the shell reads the line, so a value in a
command can become a part of that command. `env:` also keeps the body to one
line: the backend, the Nix version and the workspace path were the values that
made an expression necessary.

# Version control

This repository uses Jujutsu (`jj`) for version control. Use `jj` commands for
status, for diffs, for history, and for inspection of commits and changes. Do
not run a Git porcelain command such as `git status`, `git diff`, `git commit`,
`git checkout`, or `git reset`. Use a Git command only when the user asks for
Git, or when a tool needs Git plumbing.

**Commit each piece of work when that piece is complete and its tests pass.
Do not collect several pieces into one commit at the end of a task.** One
commit says one thing, and a reader can find the change that caused a defect.
A commit that holds a package change, a defect correction and a removal
answers no question about any of the three.

The rule is a size rule as well. When a task gives two answers that stand
alone, write two commits. `jj split <paths> -m "..."` divides the working copy
after the fact, so the size of the commit is a decision that you take at the
end and not at the start.

Finish each task on an empty commit. Run `jj new` when the last commit of the
task is complete, so the next task does not land inside it.

`pynix` is the dogfooding consumer of nanopynix. `pynix` must depend on the
public APIs of `nanopynix`. If `pynix` needs a library capability of general
use, add that capability to nanopynix, and do not import a private
implementation module. A narrow dependency on a private module is acceptable
only when a redesign is not justified. Record each such dependency in the
ledger in `tests/meta/test_consumer_surface.py`, with the reason. That test
fails until you do.

This rule was prose only until then, and 30 import sites in three packages did
not follow it. A convention that a machine can check belongs in `tests/meta/`,
which is the home of the self-checks of this repository. Put the next one
there, rather than in another paragraph of this file.

# grpclib-transports

`grpclib-transports/` is a subproject of this repository, and not a
third-party dependency. It carries gRPC over four asyncio transports: stdio
subprocess pipes, multiprocessing pipe pairs, Unix-domain sockets, and SSH
sessions. The rpc engine of nanopynix uses the first two to speak to each
worker process.

**nanopynix is its only consumer. Change the library when nanopynix needs a
different behavior.** It came from its own repository, and it arrived here so
that a change to a transport and the change to nanopynix that needs it go in
one commit. The rule above, which tells `pynix` to depend on the public API of
nanopynix, does not apply in this direction: there is no public API of
grpclib-transports to protect, and no other consumer to break.

`greeter-proto/` is the generated fixture service that the tests of
grpclib-transports call over each transport. protoc writes every module of it
(`greeter-proto/generated.nix`), so there is no `src/` in the checkout. It is
a test fixture, and no code that ships imports it.

Run the tests, and the benchmarks:

- direnv exec . timeout 60 pytest grpclib-transports # the tests only
- direnv exec . timeout 300 pytest grpclib-transports/benchmarks # the measurement run

The subproject has its own `pytest.ini`, so `rootdir` is
`grpclib-transports/` and none of the nanopynix-specific configuration
applies. The `benchmark` directory is not a correctness gate, and `testpaths`
leaves it out.

CI runs the tests as `check-grpclib-transports`, in the `static-checks` job.
Build that gate the way CI builds it:

- nix build --no-link .#check-grpclib-transports

**That gate exists because the tests would otherwise run nowhere.** The
project was a nixpkgs `buildPythonPackage` in its own repository, so
`pytestCheckHook` ran the suite inside each build. It is a pyproject.nix
builders package here, and those have no check phase.

`ruff-strict.toml` gives this subproject its own per-file ignores, and the
`TID251` entry gives the reason to read: the ban on the raw `asyncio`
primitives is right for nanopynix, which is anyio-structured, and it does not
reach a library whose subject is `asyncio.Protocol` callbacks. The `src/` of
the library still meets the rule.

**`tests/AGENTS.md` maps every directory under `tests/`.** Read it before you
add a test module. It gives the rule that picks the directory, the shape a
self-check must have, and the reason each part of that shape exists.

pytest-agent starts automatically in this environment, because it detects
`CLAUDECODE` and similar environment variables of an agent harness. Each plain
`pytest ...` invocation therefore writes the full detail of each test to
`.pytest-agent/runs-NNNN/`. This detail includes tracebacks and the captured
stdout, stderr, and logs. The terminal output does not change what pytest-agent
writes, so do not pipe pytest through `tee` or `tail` to keep the output.

When a test fails, read the detail file of that test. The list of failed and
errored tests at the end of the run gives the path to the file. The last line
of `.pytest-agent/history.jsonl` names the run. Do not use the minimal progress
lines in the terminal alone.

**Read `pytest-agent/SKILL.md` for the procedure to run pytest here and to read
the results.** That file is the source of truth for the full workflow, and this
file summarises only the first part of it. `SKILL.md` also documents these
commands:

- `pytest-agent digest` groups the failures by root cause. Start with this
  command, and do not read each failure separately first.
- `pytest-agent watch --run <label>` follows a run that is still going. It
  prints one line for each failure, stuck test, finish and death. Start a long
  run in the background, put `watch` under a `Monitor`, and then wait. Do not
  build a `tail -f` loop or a `grep` filter to do this.
- `pytest-agent history '<test>'` tells you if your change broke the test, or
  if the test failed before your change.
- `pytest-agent rerun` runs only the recorded failures again.
- `--agent-label` gives a name to a long background run, so that you can query
  the run while the run continues.
- `agent_notes` and `from pytest_agent import note` get a value out of a test,
  and also out of the code under test. Use them instead of a separate
  `python -c` command.

# pynixd

`pynixd/` is a subproject of this repository, and not a third-party
dependency. It is a Nix daemon protocol proxy and a distributed build cache,
in pure Python over AsyncSSH. It sits between a Nix client and a set of remote
builders, and it caches queries, removes duplicate builds and schedules across
backends. `pynixd/nix-daemon-protocol/` is the wire package under it, and it
is a separate distribution because it holds the codecs alone and depends on no
part of pynixd.

**It implements in Python what nanopynix binds from C++.** `pynixd/wire.py`,
`nar.py`, `drv_parser.py` and `store/daemon.py` answer questions that
`nanopynix-bindings` answers through libnixstore. That is the reason the two
trees are in one repository, and it is not a fault to correct: this suite
already speaks the real protocol to a real `nix-daemon` through
`--nix-test-backends local,daemon`, so a Python implementation of the other
end is a differential test of both. Issue #131 holds the work that integrates
the project, and it names what is decided and what is not.

**It arrived by a merge of two histories that changed no file**, so it still
carries its own conventions. Four gates of this repository excluded it, and
two of the four take it now: `check-lint` and `check-format`. `ruff-strict`
and pyright still exclude it, and each exclusion names its reason and points
at #131.

**Read `pynixd/AGENTS.md` before you change anything under `pynixd/`.** That
file is the source of truth for that project, and this file does not replace
it. Its three-tier execution pattern, its build queue and its rule of one
pytest process at a time are all in there.

**Lix is not supported.** The project supported it, through `LIX_BIN` and the
`--client-bin`, `--local-bin` and `--builder-bin` options. Every one of those
is gone. Do not add a branch for a second implementation of Nix.

# Issues, and how a commit closes one

This repository tracks work as GitHub issues. Use the `gh` CLI to read them,
and to create one.

**Put `Closes #<number>` in the commit message when the commit completes an
issue.** Write it on its own line at the end of the body, after the
Conventional Commits subject. The default branch of this repository is
`develop`, so GitHub closes the issue when the commit reaches `develop`.

```
fix(inproc): let a cancellation leave Session.close

close_resource caught BaseException, so it collected a CancelledError into
the error list. The scope that owns the cancellation never saw it.

Closes #9
```

Follow these rules:

- Use `Closes #<number>` for a commit that satisfies the whole issue. Use
  `Refs #<number>` for a commit that is one part of a larger issue. Do not
  write `Closes` for partial work, because GitHub closes the issue anyway.
- Name each issue that the commit completes. Write a separate `Closes #N` line
  for each one.
- An issue states its acceptance criteria and the tests that it needs. Meet
  both before you write `Closes`. A commit that closes an issue without the
  tests that the issue asks for is not complete.
- To find the work, read issue #26. That issue tracks the roadmap of the code
  review, and it links each item.
- `CODE_IMPROVEMENT_PLAN.md` maps a finding of the review to its issue number.
  That file holds **no status**, because the issue holds the status. Do not add
  a status column to it, and do not mark an item done there.

# Python coding conventions

- Use `from __future__ import annotations` in each Python module that defines
  or uses a type annotation.
- Do not use a string type hint such as `"Store"`. Use future annotations and
  an import in an `if TYPE_CHECKING:` block.
- Keep the imports at the top of the file. Do not write a lazy import in a
  function or in a method. A lazy import is permitted only when it is necessary
  to break a circular import cycle. To break such a cycle, first try to move
  the shared types to a neutral module.
- Use this import order:
  1. `from __future__ import annotations`
  2. imports of the standard library
  3. imports of third-party packages
  4. local `nanopynix` imports
  5. an `if TYPE_CHECKING:` block that contains only type-only imports
  6. module constants
  7. code
- To re-export a name from another module, use the explicit pattern
  `from module import Name as Name`. Put the related re-exports in one import
  block of multiple lines.
- Do not use an `assert` statement outside `tests/`. For validation at runtime,
  write `if ...: raise ...`. To satisfy the type checker, use a local variable
  alias, or an explicit `if value is None: raise ...` check.
- Do not use `asyncio.get_event_loop()`. Use `asyncio.get_running_loop()` in
  async code. For a timestamp, use `time.monotonic()`.
- Keep a strong reference to each background task that `asyncio.create_task()`
  creates. Put the reference in a `set` or in a `list` on the instance.
- Do not hide an unexpected failure with `except Exception: pass`. Log each
  unexpected exception. Use `contextlib.suppress(...)` only for an exception
  that you expect, and add a comment that tells why the exception is safe to
  ignore.
- Give a specific rule name and an inline justification for each suppression of
  a lint rule or of a type-checker rule. Use the form
  `# type: ignore[rule-name] -- reason` or `# noqa: RULE -- reason`. Do not
  write a blanket suppression, and do not write a suppression with no reason.
- Use the anyio primitives in new code, and do not use the raw `asyncio`
  equivalents. These primitives include `anyio.Lock`, `anyio.Event`, the memory
  object streams, `anyio.fail_after`, `anyio.move_on_after`,
  `anyio.create_task_group`, `anyio.open_process`, `anyio.to_thread`, and
  `anyio.from_thread.BlockingPortal`. Two exceptions are intentional, and this
  file documents both:
  1. `_core/_nix_executor.py` calls `asyncio.wrap_future`. This call operates
     with a dedicated `concurrent.futures` thread that already runs. A route
     through `anyio.to_thread` would use a slot in the shared capacity limiter
     of anyio for no benefit.
  2. `asyncio.create_task()` hosts a `CancelScope` or a `TaskGroup`, which is a
     plain `anyio.create_task_group()` or an
     `anyio.from_thread.BlockingPortal`, when different tasks call `start()`
     and `close()`. Two separate gRPC handler calls do this in
     `rpc/worker/_worker_primop.py`. The same task must enter and exit a
     `CancelScope` or a `TaskGroup` of anyio.

# Banned patterns

- **In an async function, always use the async alternative when one exists.
  Never use a blocking sync call.** This rule also applies to test code.
  - Replace `subprocess.run`, `subprocess.Popen`, and `os.system` with
    `anyio.open_process`. `anyio.open_process` sets `stdin`, `stdout`, and
    `stderr` to `PIPE` by default, and asyncio does not. Pass `None` for each
    of these three arguments to inherit the terminal.
  - Replace blocking `pathlib.Path` I/O with `anyio.Path`. This I/O includes
    `.read_text()`, `.write_text()`, `.exists()`, and `.mkdir()`. `anyio.Path`
    has the same API. Such a call is easy to miss, because it does not look
    like a blocking call.
  - Pure manipulation of a path with no access to the file system stays a plain
    `pathlib.Path`.

# Writing style: ASD-STE100

Write all descriptive English in this repository in Simplified Technical
English (ASD-STE100). This applies to documentation, READMEs, commit messages,
docstrings, code comments, error messages, and pull request descriptions. It
does not apply to code itself, to identifiers, or to quoted command output.

Follow these rules:

- Use one topic for each sentence. Keep a descriptive sentence to 25 words or
  fewer, and an instruction to 20 words or fewer.
- Use the active voice. Write "the worker sends the event", not "the event is
  sent by the worker".
- Give an instruction as a command. Write "Run the tests", not "The tests
  should be run" or "You may want to run the tests".
- Use the simple present, past, or future tense. Do not use a perfect tense
  ("has changed", "had run") when a simple tense is sufficient.
- Use each technical word with one meaning only, and always use the same word
  for the same thing. Do not write "evaluator" in one paragraph and "evaluation
  engine" in the next paragraph.
- Do not use a noun as a verb, and do not use a verb as a noun.
- Keep a noun cluster to three words or fewer. Write "the cache of the worker
  process", not "the worker process result cache".
- Keep the articles ("a", "the") and the relative pronouns ("that", "which").
  Do not remove them to save space.
- Write out what a pronoun refers to when the reference is not immediate.
  Replace "it does this because it is faster" with the actual subjects.
- Keep a descriptive paragraph to six sentences or fewer. Start the paragraph
  with the topic sentence.
- Write the steps of a procedure in the order that the reader must do them.
- Do not use slang, an idiom, or jargon that this repository does not define.
  Do not use humour, and do not ask a rhetorical question.
- Put a warning or a caution before the step that it applies to, and start the
  warning with the command.

A commit message keeps the Conventional Commits prefix, for example
`feat(scope):` or `fix(tests):`. ASD-STE100 applies to the text after the
prefix and to the body.

This file follows these rules, and it is the example to copy. Apply the rules
to text that you write or change. Do not rewrite other existing prose only to
make that prose comply.

# Design notes

**In Nix, the term "stderr" means logging. It does not mean the stderr of the
operating system.** Nix uses the term "stderr" for the log events of
`nix::Logger`. These events already flow through the RPC pipe between the
worker and the master, as `action: "msg"` events and `action: "error"` events.
The IPC of the worker uses stdin and stdout only, for the JSON-RPC protocol.
The subprocess inherits file descriptor 2 from the parent.

Do not add a separate stderr pipe. Such a pipe is redundant, and it confuses
the logging abstraction of Nix with the stderr of the operating system.

# The supported Nix versions

**`supportedNixFloor` in `default.nix` names the oldest Nix that this
repository supports. It is 2.34.** One number moves the whole matrix, because
every variant reads it. The versions that CI builds are 2.34, 2.35 and `git`.

Two rules follow from it:

- **Do not add a version branch to library code to keep an old Nix alive.**
  Gate the test instead, and give the gate the upstream defect and the issue
  to read. `nanopynix_testing.nix_markers` holds the markers, and each one
  names what it excludes and why.
- **Do not write a gate that the floor already answers.** A marker such as
  `minimum="2.32"` can never skip a test when the floor is 2.34, so it reads
  as a live constraint and is not one. Issue #126 removed nine of those.

Raise the floor when an old version costs more than it reports. Issue #126 is
the example to copy: it measured the skips, the run time and the gates that
the version alone reached, and it recorded what the removal cost as well as
what it saved.

# Test failure discipline

Do not assume that a failed test is unrelated, flaky, or pre-existing.

When a test fails after your change, start with this assumption:

> "My change caused this failure, or my change made this failure visible."

Call a failure pre-existing only after you prove that it is pre-existing.

## Required procedure for a failing test

When a test fails:

1. Run the exact failing command again, and confirm the failure.
2. Inspect the failure carefully before you make a claim about it.
3. Decide if your recent changes can affect the behavior that failed.
4. Use `jj diff` to review each file that you changed.
5. To support a claim that the failure is unrelated, do one of these three
   things:
   - Revert your changes, and show that the test still fails.
   - Run the same test on a clean baseline branch or commit.
   - Find a CI record or a test record of the same failure from before your
     work.

Do not write one of these statements until you complete one of those three
checks:

- "This is pre-existing"
- "The tests are broken"
- "This is unrelated"
- "This is likely flaky"
- "The failure is outside the scope"

Write this instead:

> "I have not proven that this failure is unrelated. I will continue to debug
> it under the assumption that my change caused it."

## Pytest output discipline

pytest-agent starts automatically in this environment, and it enforces this
rule. pytest-agent refuses to run when it detects a pipe from its own stdout
into `head`, `tail`, `grep`, `sed`, `awk`, or a similar reader. It then exits
with code 2 before it collects a test. pytest-agent always writes the full
detail of each test to `.pytest-agent/runs-NNNN/`, and this detail includes
tracebacks and the captured stdout, stderr, and logs. The terminal output does
not change what pytest-agent writes.

Let the output of pytest go to the terminal with no filter. A pipe into `tee`
is not necessary to keep the evidence.

When the minimal terminal output of pytest is not sufficient to explain a
failure, read the detail file of the test. The list of failed and errored tests
at the end of the run gives the path. The log of a test is also at
`.pytest-agent/runs-NNNN/<test file path>/<test name>.log`, under the run that
the last line of `.pytest-agent/history.jsonl` names. Do not run the tests
again with a shell filter.

The refusal also applies to a renamed grep, because pytest-agent identifies the
reader by `argv[0]`, by `comm`, and by `/proc/<pid>/exe`. The `grep` shim of
this harness shows as `ugrep (running as .claude-wrapped)`. This shim is not a
different tool, and you must not use it to get around the rule. Remove the
pipe.

A pipe into `tee` or into `wc -l` is permitted. A run that only makes a list is
exempt, for example a run with `--collect-only` or with `--fixtures`. Read
`pytest-agent/SKILL.md` for the full rule, and for the query commands that
replace a filter.

## Never hide a failure

Do not change a test only to make it match broken behavior.

Change a test only when all three of these conditions are true:

- The intended behavior changed.
- The old expectation of the test is obsolete, and you can show this.
- You explain the reason clearly.

Do not weaken an assertion, skip a test, delete coverage, or loosen error
handling to make a test pass. Do these things only with an explicit
justification.

## Expectations for debugging

Use small steps, and base each step on evidence:

- Reproduce the failure.
- Isolate the smallest test that fails.
- Inspect the related code path.
- Add temporary logging only when the logging helps you to identify the
  problem.
- Remove the temporary logging before you finish.
- Make the smallest change that corrects the root cause.
- Run the failing test again.
- Then run the larger set of related tests.

## Reports of a failure

When you report a test failure, include this information:

- the exact command that you ran
- the exact name of the test that failed
- the error message or the assertion message
- whether the failure occurred again after your change
- why your correction addresses the root cause

When you think that a failure is pre-existing, include the proof. A suspicion
is not a proof.
