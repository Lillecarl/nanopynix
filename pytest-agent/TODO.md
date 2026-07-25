# pytest-agent TODO

## Tracked debt

`noqa: C901` / `noqa: PLR0913` comments in this package point here. The
complexity and argument-count suppressions in `_pipe_guard.find_banned_pipe_reader`,
`AgentRuntime.__init__`, and `tests/test_capture.py::_report` are accepted for
now rather than restructured.

## Why notes print to the terminal

`note()`/`attach()` (see `_notes.py`) are the one thing agent mode prints that
isn't about pass/fail, which is a deliberate exception to "nothing goes to the
terminal that could go to a file". The reason is turns: a value only readable
from a file costs a second turn to go and read it, and the point of a probe is
to answer the question in the run that added it. It stays honest because a
note is explicit -- nobody gets note output they didn't ask for -- and the
block is capped at `MAX_NOTE_LINES` so a probe inside a parametrized loop
can't push the failure list off the screen.

Notes are appended to `notes.jsonl` as they are taken rather than buffered
until the test finishes, because the runs worth probing are exactly the ones
that don't finish: a segfault, an `os._exit`, a killed hang. `_capture.Note`
is the only way one is constructed, so every value is JSON-safe (by `repr`
if it has to be) before it reaches anything.

The other half of the intent is to make a throwaway *test* cheaper to write
than a throwaway `python -c`: the test gets the project's fixtures, its
environment, `profile`, and a place to put its output, and answers in the
summary. See the README section.

## Not supported: pytest-xdist

Untested and expected broken, documented in the README rather than fixed.
`pytest_configure` runs in every xdist worker, so each worker claims its own
`runs-NNNN` via `next_run_dir()`: one logical run scatters into N partial run
directories and N `history.jsonl` entries, and every query answers from
whichever worker won the highest number. Fixing it means recording per-worker
(`workerinput` identifies them) and merging in the controller -- the controller
being the only process that sees the whole run, and the one whose terminal
output agent mode silences.

Deliberately deferred: xdist isn't in this repo's dependency tree, and a
merge protocol is a real design rather than a patch.

## Testing approach

The suite is recursive -- inner `pytest` sessions via `pytest.Pytester` and
inner CLI invocations via `conftest.run_cli`, both against throwaway project
directories. No env var disables the plugin for its own tests and none is
wanted: `conftest._clean_agent_env` (autouse) gives every inner process an
empty pytest-agent environment, and the pipe guard only ever inspects the
inner process's own stdout. See `tests/test_agent_workflow.py` for the
end-to-end path, and `tests/test_notes.py` for the note/attachment surface
(including a test that kills its own process to prove a note outlives it).

`tests/test_interrupt.py` goes further and signals a real pytest subprocess:
SIGTERM survival and stack dumps can't be exercised in-process, since the
session under test and the session running the test are the same process.
Its handler-not-clobbered and kill-survival tests were each confirmed by
mutation -- removing the `SIG_DFL` guard, and skipping the handler install --
before being kept.

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
