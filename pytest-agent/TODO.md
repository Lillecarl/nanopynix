# pytest-agent TODO

## Tracked debt

`noqa: C901` / `noqa: PLR0913` comments in this package point here. The
complexity and argument-count suppressions in `_pipe_guard.find_banned_pipe_reader`,
`AgentRuntime.__init__`, and `tests/test_capture.py::_report` are accepted for
now rather than restructured.

## Run labels: one source of truth, deliberately

`--agent-label` is resolved from `meta.json` and nowhere else, even though
`summary.json` and `history.jsonl` also carry the label. Those two are written
at `sessionfinish`, so a resolver that consulted them would fail to find
labels on exactly the runs the archive exists to explain -- the ones killed
mid-run -- and would not find a labeled run at all until it finished, which
defeats the purpose of naming a long background run. The copies in the two
end-of-run records are for reading, not resolving.

Both resolving a label and pruning scan every run directory and read its
`meta.json`: O(runs on disk) per query, bounded by `--agent-keep-runs`
(default 20, plus the labeled budget). No index is kept, because an index is
another file two concurrent runs would have to agree on -- the failure mode
the `.lock` work was about. If the default keep count ever grows by an order
of magnitude, this is the thing to measure first.

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

## What we borrow from pytest, and what we deliberately don't

`runs-NNNN` is a numbered-directory scheme with pruning, which is exactly what
pytest already does for `/tmp/pytest-of-$USER`. Reusing that machinery was
considered and rejected:

- The only public way to redirect it is `--basetemp`, and `getbasetemp()`
  does `if basetemp.exists(): rm_rf(basetemp)` unconditionally, with no lock
  check. Pointing it at the agent directory would delete the archive at the
  start of every session, leaving `history` and `compare` nothing to read --
  and would relocate the user's own `tmp_path` fixtures into our directory.
- The helpers underneath (`make_numbered_dir_with_cleanup`,
  `create_cleanup_lock`, `LOCK_TIMEOUT`) live in `_pytest.pathlib`; `pytest`
  re-exports none of them. Depending on them is the opposite of being a good
  citizen of the plugin API.
- The semantics differ. Those directories are ephemeral scratch governed by
  `tmp_path_retention_count`/`policy` -- and `policy = "none"` sets `keep = 0`.
  Ours is an archive with its own `--agent-keep-runs`. Coupling them means a
  project setting `tmp_path_retention_policy = "failed"` silently loses its
  pytest-agent history on green runs.

What we *did* take is the convention: a `.lock` file in the directory a
session is still writing to, honored by `prune_old_runs` (see
`_history.create_run_lock`). The pruning hazard is identical -- "keep the
newest N by number" can delete a live, lower-numbered concurrent run -- and
`protect` only ever covered the pruning session's own directory.

`--agent-stuck-after` overlaps pytest's `faulthandler_timeout` ini option,
which was found only after the fact. It is kept because it writes to a file
beside the test's log rather than to stderr, repeats, and is visible to the
query commands; pytest's fires once, to stderr, but via a C timer that works
even when the GIL is never released. Ours deliberately does not touch
`faulthandler`'s process-global `dump_traceback_later` timer, so enabling both
does not silently disable one. The README says which to reach for.

Silencing the terminal reporter is the one place where agent mode can stop
being a good citizen without anyone noticing. It works by swapping the
reporter's output file, and for a long time that file was `os.devnull` --
which quietly destroyed every *other* plugin's output too, since they all
report through the same writer. `--cov`, `--durations` and `--junit-xml` all
produced nothing, with no error. It is a file now, and the sections written
by plain `pytest_terminal_summary` hookimpls are printed back out. The
selection is structural rather than a list of known plugins: a
`wrapper=True, trylast=True` hookimpl is the innermost wrapper, so its window
contains exactly the non-wrapper impls and none of TerminalReporter's own
before/after writes. Any new plugin is covered without pytest-agent knowing
it exists.

Other overlaps, accepted as-is: `note()` against `record_property` (which
only reaches junit-xml), `index.jsonl` against `--junit-xml` and
pytest-reportlog (raw report dumps rather than a curated per-test record),
and `rerun` against `--lf` (a single-slot cache rather than an archive).

## Agent mode must never be why a run fails

Everything this plugin does happens inside somebody else's test session, in
hooks whose exceptions are fatal to it: `pytest_configure` (an INTERNALERROR
before a single test runs) and `pytest_runtest_logreport` (an INTERNALERROR
that abandons every test after the current one). Both were reachable through
ordinary bad luck, and both were found by dogfooding rather than review:

- a parametrized id over NAME_MAX, which pytest itself runs fine
- a `.pytest-agent` that cannot be written -- read-only checkout, read-only
  CI workspace, a directory left root-owned by a container, a full disk

The rule the code now follows: agent mode is a way of watching a test run, so
it degrades rather than obstructs. Unwritable at startup means agent mode
turns itself off and pytest proceeds untouched; unwritable mid-run means the
outcome is still recorded and the reason is reported as `capture_error` (per
test) or `index_error` (once for the run). Always on stderr or in the closing
block, never silently -- a run that quietly stopped recording is a trap,
because the next `pytest-agent last-failures` answers from a stale run.

Anything new that touches the filesystem from inside a hook belongs behind
the same discipline.

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

The suite sets `PYTEST_DEBUG_TEMPROOT` to a temp root of its own (see
`tests/conftest.py`). Sharing `/tmp/pytest-of-$USER` with the surrounding
project's suite means the two prune each other's numbered directories by
number, and this suite has already been killed that way mid-run: 93 tests
failed at once on a base temp directory that had been deleted out from under
the live session.

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
