# pytest-agent TODO

## Tracked debt

`noqa: C901` / `noqa: PLR0913` comments in this package point here. The
complexity and argument-count suppressions in `_pipe_guard.find_banned_pipe_reader`,
`AgentRuntime.__init__`, and `tests/test_capture.py::_report` are accepted for
now rather than restructured.

## Testing approach

The suite is recursive -- inner `pytest` sessions via `pytest.Pytester` and
inner CLI invocations via `conftest.run_cli`, both against throwaway project
directories. No env var disables the plugin for its own tests and none is
wanted: `conftest._clean_agent_env` (autouse) gives every inner process an
empty pytest-agent environment, and the pipe guard only ever inspects the
inner process's own stdout. See `tests/test_agent_workflow.py` for the
end-to-end path.

## Resolved

Friction observed while an agent used pytest-agent heavily for a debugging /
audit session (CIP3 error-pipeline work). All four items are now fixed; kept
here as the record of *why* these behaviors exist.

### 1. Pipe guard fired on `--collect-only`, where there was no detail to lose

`pytest --collect-only -q | tail` was refused, and the workaround (redirect to
a file, then `tail` the file) gave the agent the identical truncated view with
extra ceremony.

Fixed: the guard is skipped for runs that print a listing and execute no test
body -- `--collect-only`, `--fixtures`, `--fixtures-per-test`, `--markers`,
`--setup-plan`, `--help`, `--version` (`_pipe_guard.zero_detail_mode`).
`--setup-only` is deliberately *not* exempt: it really does execute fixtures.
No `pytest-agent count` subcommand was added -- `pytest --collect-only -q |
tail -1` now answers that question directly, so a subcommand would be
redundant surface.

The refusal message now also states that the guard is independent of agent
mode and that `PYTEST_AGENT_NO_AUTODETECT=1` does not turn it off, which is
what sent the agent down the "did the env var not take effect?" path. It
deliberately does *not* name `--agent-allow-pipe`: telling an agent how to
bypass the guard turns "stop truncating" into "keep truncating, with a flag".

### 2. No addressable way to read one test's detail

Hand-assembling
`.pytest-agent/runs-0259/tests/.../test_hover_...[in_process-local].log`
required knowing the run number, mirroring the test file path, and quoting
`[`/`]`.

Fixed: `pytest-agent show '<nodeid or unique substring>'`, plus
`pytest-agent last-failures [--detail]`. Both take `--run N` and `--dir PATH`,
default to the newest run with an `index.jsonl`, and find `.pytest-agent` by
searching upward from the cwd. Queries never go through `pytest.main()` --
their own output is a legitimate thing to pipe into `grep`.

### 3. Print the resolved log path next to each failed test

Fixed, as one line per failure rather than two: the log path alone, since it
*is* the nodeid with `::` written as a separator. The nodeid is appended in
parentheses only when the on-disk mapping lost something (a `/` in a
parametrized id is sanitized to `_`; collect-error logs use another scheme).
Paths are `shlex.quote`d -- fish refuses an unquoted `[...]` path outright.

### 4. Wanted a "just the cause" digest across failures

Fixed: `pytest-agent digest` groups failures by normalized exception message
(store hashes, temp dirs, addresses and numbers normalized away) and prints,
per group, the count, one real un-normalized message, the crash location, the
traceback frames in first-party code, and the nodeids.

This needed structured data at record time, not log re-parsing: failing
records in `index.jsonl` now carry `crash` (exc type, message, file, line) and
`frames` (up to the 20 innermost locations, each tagged `first_party`) --
see `_crash.py`. "First-party" means under rootdir *and* not inside a
`site-packages`/`dist-packages` tree, so a vendored `.venv` inside the project
doesn't count as your code.

Runs written before this landed have no `crash`/`frames` (up to
`--agent-keep-runs` of them survive an upgrade); the queries degrade to
"no crash info recorded" rather than failing.
