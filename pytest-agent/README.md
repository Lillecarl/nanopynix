# pytest-agent

A pytest plugin that makes pytest runs friendly to an AI agent driving them
from a terminal: minimal, low-noise CLI output, and everything an agent could
need written to disk instead, split per test file and test name.

This lives inside the nanopynix repo for now but has no dependency on it; it
may get extracted into its own repo later.

`SKILL.md` beside this file is the same material aimed at an agent rather than
at a reader: what to run, in what order, and which surprises to expect. Point
an agent harness at it (e.g. copy or symlink it into `.claude/skills/`) and
this README stays the reference.

## Usage

```sh
pytest --agent
```

`--agent` also turns on by itself, with no flag needed, when a known AI
coding-agent harness env var is present (`CLAUDECODE`, `CURSOR_AGENT`,
`GEMINI_CLI`, `CODEX_SANDBOX`, `AI_AGENT`, and others -- see
`_harness_detect.py` for the full list). Set `PYTEST_AGENT_NO_AUTODETECT=1`
to turn that off and require an explicit `--agent`/`PYTEST_AGENT=1`.

The CLI prints a directory path at the start and end of the run and, every
`--agent-heartbeat` seconds while tests are running, one line like:

```
[pytest-agent] 42s pass=118 fail=2 done=120 tot=311 cur=tests/test_foo.py::test_bar
```

That's it -- nothing else prints while tests run. Whether something is
progressing or stuck is visible from that line alone: the counts and `cur`
change between prints if things are moving, and elapsed keeps climbing with
nothing else changing if they aren't.

At the end, each failure gets exactly one line: the path to its log, resolved
and shell-quoted so it can be read back directly.

```
[pytest-agent] done in 42.4s -- 118 passed, 2 failed, 0 error, 0 skipped, 0 collection errors
[pytest-agent] 2 failed/errored:
[pytest-agent]   .pytest-agent/runs-0002/tests/test_foo.py/test_bar.log
[pytest-agent]   '.pytest-agent/runs-0002/tests/test_foo.py/test_p[a_b].log'  (tests/test_foo.py::test_p[a/b])
[pytest-agent] shared root cause? pytest-agent digest
```

The nodeid isn't printed separately, because the path already is one -- with
`::` written as a directory separator. It's appended in parentheses only when
that mapping lost something (a `/` inside a parametrized id becomes `_` on
disk), so the line is never ambiguous and never redundant.

Everything else (per-test stdout/stderr/log/tracebacks, an index, a run
summary) is written to disk for you or an agent to read directly. Each
invocation gets its own numbered run directory under `--agent-dir`
(`.pytest-agent` by default), so nothing from a previous run is ever
overwritten:

```
.pytest-agent/
  history.jsonl             # one line per run, appended when it finishes --
                             # duration, counts, hostname, git rev, etc.
  runs-0001/
    meta.json                 # what this run *is*, written before the first test:
                               # run number, --agent-label, start time, pid, argv
    index.jsonl              # one JSON record per test, appended as it finishes;
                             # failures also carry `crash` (exception type,
                             # message, file:line) and `frames` (traceback
                             # locations, each tagged first-party or not);
                             # `capture_error` on the rare test whose detail
                             # file could not be written, saying why
    notes.jsonl               # one line per note() call, appended as it happens
                             # (only when a test recorded something -- see below)
    summary.json              # the same fields as this run's history.jsonl line
    terminal.txt              # what plain pytest would have printed to the
                               # terminal, verbatim -- agent mode redirects the
                               # builtin reporter here rather than discarding it
    reports.txt               # just the end-of-run reports other plugins wrote
                               # through it (a --cov table, --durations), lifted
                               # back out of that transcript on their own
    collect_errors/          # one log per module that failed to import/collect
    tests/test_foo.py/
      test_bar.log            # nodeid, outcome, duration, notes, traceback,
                               # captured stdout/stderr/log, one section each
      test_bar.json            # the same record that's in index.jsonl
      test_bar.files/          # whatever the test attached, if anything
      test_bar.stuck.txt       # every thread's stack, dumped while this test was
                               # still running (only if it ran long enough --
                               # see --agent-stuck-after)
    .lock                      # present only while this run is in progress, so a
                               # concurrent run's pruning leaves it alone
  runs-0002/
    ...
```

pytest-agent always prints the exact run directory in its startup and final
banner lines (`run 2, pid 12345: writing full per-test detail to: ...`) --
that's the
normal way to find where to look. If you need to find the most recent run
without having seen that banner, `history.jsonl`'s last line always names it:

```sh
tail -n1 .pytest-agent/history.jsonl | jq -r .run_dir
```

There's deliberately no mutable "latest" pointer (e.g. a symlink) kept in
sync on every run -- under concurrent invocations against the same
`--agent-dir`, anything that swaps the same path each time is a class of race
worth avoiding for a "which was newest" convenience that `history.jsonl`
already gives you for free.

Track a test's duration across runs:

```sh
jq -c 'select(.nodeid == "tests/test_foo.py::test_bar") | .duration_s' .pytest-agent/runs-*/index.jsonl
```

## Reading a run

`pytest-agent` doubles as a query tool over what the last run wrote. These
subcommands never start a pytest session -- they only read `index.jsonl` and
the per-test logs, so they're safe to pipe anywhere.

| Command | Answers |
| --- | --- |
| `pytest-agent last-failures` | Which tests failed, and where each one's detail is |
| `pytest-agent last-failures --detail` | ...with every failure's full log inlined |
| `pytest-agent show '<nodeid>'` | One test's full detail, by nodeid or any unique substring of one |
| `pytest-agent digest` | Failures grouped by root cause, so 18 failures sharing one bug read as one entry |
| `pytest-agent watch` | Follows a run that is still going, and prints one line per failure, stuck test, finish or death |
| `pytest-agent history '<nodeid>'` | That test's outcome in every run still on disk -- did I break this, or was it already failing? |
| `pytest-agent compare [A B]` | What changed between two runs: newly failing, newly passing, still failing |
| `pytest-agent rerun` | Re-run exactly the tests that failed, without re-running the suite |
| `pytest-agent help` | The above, with flags |

`show`, `last-failures` and `digest` take `--run N` to read an older run
instead of the newest, or `--run LABEL` to read one by name (see *Naming a
run* below). `history` and `compare` read across runs instead, so `history`
has no `--run` -- `compare` names its two runs positionally, and takes a label
in either position. All of them take `--dir PATH` to point at an agent
directory other than the nearest `.pytest-agent` at or above the current
directory.

```sh
$ pytest-agent digest
runs-0259 (.pytest-agent/runs-0259): 18 failed/errored of 311 recorded, 1 distinct root cause

[1] 18x  FileNotFoundError: /nix/store/8jz...-swagger.json
     at src/pynix/openapi.py:88
     first-party frames, outermost first:
       tests/pynix/test_lsp_scenarios.py:140
       src/pynix/openapi.py:88
     - tests/pynix/test_lsp_scenarios.py::test_hover_on_a_kind_name[in_process-local]
     ...
```

`digest` groups by the exception message with the parts that vary per test
normalized away (store hashes, temp directories, addresses, numbers), so
failures that share a cause collapse into one entry with a count -- while the
message shown is a real, un-normalized one from the group. Each group lists
the traceback frames in your own code, with stdlib, site-packages, and any
vendored `.venv` inside the project filtered out.

Runs recorded by an older pytest-agent have no structured crash data; the
queries still work on them and say so rather than failing.

### Naming a run

A run started with `--agent-label` can be asked about by that name from then
on, instead of by a run number nobody knows in advance:

```sh
# in the background -- this one takes 10 minutes
$ pytest --agent --agent-label full-suite tests/

# meanwhile, focused runs, as many as it takes
$ pytest --agent tests/test_parser.py
$ pytest --agent tests/test_parser.py -k roundtrip

# and afterwards, whichever run number the long one turned out to be
$ pytest-agent last-failures --run full-suite
runs-0261 [full-suite] (.pytest-agent/runs-0261): 4 failed/errored of 3106 recorded
$ pytest-agent rerun --run full-suite
```

This is what makes running the slow suite in the background practical. Without
a name, coming back to it means knowing how many runs happened while it went,
which is exactly what a background run makes unknowable.

Some details worth knowing:

- **The name works while the run is still going.** `meta.json` is written
  before the first test, not at the end. Querying an unfinished run answers
  from what it has recorded so far and says so on stderr:
  `runs-0261 is still running -- its records are incomplete`.
- **Labeled runs get their own retention budget**, the same size as
  `--agent-keep-runs`, on top of the general one. Twenty focused runs while a
  labeled suite goes would otherwise push it out of the rotation before anyone
  asked about it. A label is not immortality, though: label everything and the
  labeled rotation prunes like any other.
- **A label is never a number.** `--agent-label 42` is refused up front, so
  `--run 42` always means run 42.
- **Labels needn't be unique.** Re-running the same command with the same
  label is a normal thing to do; the newest match wins, and says so on stderr
  when there was more than one.
- **`PYTEST_AGENT_LABEL`** does the same for a whole shell or CI job.

### Watching a run in progress

The commands above answer a question about a run that has already happened.
`watch` answers "tell me when something happens", which is what a suite
started in the background actually needs. Without it, the caller must guess
when to look, and every guess is either too early or too late.

```sh
# start the suite, and leave it going
$ pytest --agent-label bg1 tests/ &

# then follow it. This ends when the run ends.
$ pytest-agent watch --run bg1
pytest-agent: watching .pytest-agent/runs-0042
FAIL  tests/test_parser.py::test_roundtrip -- AssertionError: 3 != 4
STUCK 124s tests/test_net.py::test_timeout -- stack: .pytest-agent/runs-0042/tests/test_net.py/test_timeout.stuck.txt
DONE  runs-0042 [bg1]: 4 failed, 835 passed, 10 skipped in 214s (exit 1) -- pytest-agent digest --run bg1
```

Four events, and the fourth is the one that makes the other three worth
trusting:

- **`FAIL` / `ERROR`** — one test finished badly, named with its crash
  message. `ERROR` covers a collection error too: both mean the test never
  got to say anything about itself.
- **`STUCK`** — one test has been running past `--stuck-after` (120s), and
  again at each doubling, four times at most. When the run also wrote a stack
  dump, the line names it.
- **`DONE`** — the run finished, with its counts and its exit status.
- **`DIED`** — the process is gone and wrote no summary: a segfault, an OOM
  kill, a `kill -9`.

**Without `DIED`, silence would mean nothing.** A crashed run and a healthy
one both produce no output, so a watcher that reports only good news is a
watcher you cannot leave alone. With it, silence means the run is fine.

The exit status tells the four apart: `0` finished clean, `2` finished with
failures, `3` died, `1` could not watch at all.

Some details worth knowing:

- **Nothing here parses terminal output.** Every event comes from a file the
  run writes as it goes — `index.jsonl` for finished tests, `status.json` for
  the test running now, `summary.json` for the end. So it does not matter how
  the run was started or where its output went.
- **`--wait` (60s) lets the watcher be armed first.** A run claims its
  directory inside `pytest_configure`, which is after the interpreter, the
  plugins and every conftest have loaded, so a watcher started at the same
  moment routinely gets there first. The wait covers the `.pytest-agent`
  directory as well, which the first run of a fresh checkout creates.
- **With no `--run`, it follows the newest run that is still going**, not the
  newest run. The newest is right for every other subcommand, because those
  answer about a run that is over; here it would report the *previous* suite
  as finished, immediately.
- **A reused label prefers the run that is still going**, for the same
  reason. Labels are meant to be reused, so `--run nightly` regularly matches
  both last night's run and tonight's. A name that matches only finished runs
  is taken after a few seconds, which is long enough for a run being started
  right now to claim its directory.
- **A flood of failures is capped at ten lines.** Past that they are counted
  rather than listed, and the totals arrive with `DONE`. This protects the
  last line, which is the one that matters most.
- **`--stuck-after` is not `--agent-stuck-after`.** This one decides when to
  report; that one decides when the run dumps every thread's stack into the
  run directory. Reporting should come first, so this default is lower.
- **Under `-n`, no stuck test is reported.** Several tests run at once, so
  the running one names an arbitrary member of that set and its age measures
  nothing.

#### With an AI coding agent

`watch` prints one line per event and then exits, which is the shape an agent
harness consumes: each line becomes a notification, and a command that ends
by itself stops being armed once the thing it watched is over. In Claude Code
that is a background `Bash` for the run and one `Monitor` for the watcher:

```
Bash(run_in_background=true):  pytest --agent-label bg1 tests/
Monitor(command="pytest-agent watch --run bg1 --wait 120", timeout_ms=3600000)
```

Give the monitor a timeout longer than the suite. The background run keeps
its own exit status, so the watcher reports the run and the harness reports
the process.

### Re-running the failures

```sh
$ pytest-agent rerun
re-running 18 failed from .pytest-agent/runs-0259
[pytest-agent] run 260, pid 481922: writing full per-test detail to: ...
```

`rerun` is the one subcommand that starts a pytest session: it reads a
recorded run's failures and passes those nodeids to `pytest --agent`. Anything
it doesn't recognize goes to pytest too, so `pytest-agent rerun -x` and
`pytest-agent rerun --agent-stuck-after 30` work as expected.

pytest's own `--lf` does the common case and needs no plugin. Reach for
`rerun` when `--lf` can't help:

- **`--run N|LABEL` re-runs an older run's failures.** pytest's cache holds
  only the last run in a rootdir, so the first `-k`-filtered or `-x` re-run
  overwrites the list you were working through. Every run on disk keeps its
  own, and a labeled one can be named without counting.
- **The ids never touch a shell.** `test_hover[in_process-local]` goes
  straight into pytest's argv; nothing has to quote the brackets.
- **A run with no failures re-runs nothing** and says so, rather than falling
  through to the whole suite.

Because the re-run is itself a recorded run, `pytest-agent compare` then shows
exactly what the fix changed.

### Is this failure mine?

`history` and `compare` answer the question the single-run commands can't.
Every run is already on disk; these read across them instead of re-running an
old revision to find out.

```sh
$ pytest-agent history test_hover_on_a_kind_name
3 runs on disk (runs-0257..runs-0259); older runs are pruned, so this is not the full history

tests/pynix/test_lsp_scenarios.py::test_hover_on_a_kind_name[in_process-local] -- failed in 2 of the 3 runs
  runs-0259  failed      2.13s
      FileNotFoundError: /nix/store/8jz...-swagger.json
  runs-0258  failed      2.09s
      FileNotFoundError: /nix/store/8jz...-swagger.json
  runs-0257  passed      1.98s

$ pytest-agent compare
runs-0258 -> runs-0259: 1 newly failing, 3 newly passing, 17 still failing (311 tests in both runs)
newly failing:
  tests/pynix/test_store.py::test_add_to_store
    AssertionError: assert 0 == 1
```

Both are honest about their limits. The first line says how many runs were
actually read: `--agent-keep-runs` deletes old `runs-*` directories, so
"failed in 2 of the 3 runs" means three runs *still on disk*, not three runs
ever. A test only some runs executed is counted against the runs that ran it
("failed in 1 of the 1 runs that ran it"), not against all of them.

`compare` with no arguments takes the two newest runs. Give it two run numbers
to compare any others -- in either order, so `compare 259 258` reports the same
change from the other side. Tests present in only one of the two runs (a
filtered `-k` re-run, say) are counted, not listed.

The first argument decides: `show`, `last-failures`, `digest`, `history`,
`compare`, `rerun`, and `help` are
subcommands, and anything else is forwarded to pytest with `--agent`
(`pytest-agent -x tests/` is `pytest --agent -x tests/`). A path that happens
to collide with a subcommand name still works as `pytest-agent ./show`, and a
first argument that is neither a path nor a subcommand but looks like a
misspelling of one (`pytest-agent lastfailures`) is named as such rather than
handed to pytest to fail as a missing test path.

`python -m pytest_agent ...` is the same CLI, for when the console script
isn't on `PATH`.

Or read the files directly, without the CLI:

```sh
jq -c 'select(.outcome == "failed" or .outcome == "error")' .pytest-agent/runs-0002/index.jsonl
cat .pytest-agent/runs-0002/tests/test_foo.py/test_bar.log
```

## Extra output while troubleshooting

A `print()` in a test already lands in that test's log. What it can't do is be
queried across a whole run, hold something too big to read as a log line, or
come from five frames inside the code under test. That's what notes are for.

```python
def test_resolution(agent_notes):
    agent_notes.note(store=store_path, backend=backend)  # structured, queryable
    agent_notes.attach("payload.json", raw_response)  # too big for a line
    (agent_notes.dir / "dump.bin").write_bytes(blob)  # or write files yourself
```

```python
from pytest_agent import note  # no fixture; callable from anywhere at all


def resolve(digest):  # ...including the code under test
    note(resolving=digest)
```

Everything a note reaches, it reaches at once:

| Where it lands | What that's for |
| --- | --- |
| the end-of-run summary | reading it in the same turn that ran the test |
| `notes.jsonl` in the run directory | one line per note, appended as it's taken -- a probe survives the crash it was added to investigate |
| `index.jsonl`, as `notes` | `jq 'select(.notes.backend == "daemon")' .pytest-agent/runs-*/index.jsonl` |
| the test's `.log`, above the traceback | reading one failure with its probe in context |

```
[pytest-agent] done in 3.1s -- 2 passed, 1 failed, 0 error, 0 skipped, 0 collection errors
[pytest-agent] 1 failed/errored:
[pytest-agent]   .pytest-agent/runs-0004/tests/test_lsp.py/test_hover.log
[pytest-agent] notes:
[pytest-agent]   tests/test_lsp.py::test_hover
[pytest-agent]     resolving=0lqkw72fqnp7q1kr
[pytest-agent]     attached: .pytest-agent/runs-0004/tests/test_lsp.py/test_hover.files/payload.json
[pytest-agent]   tests/test_lsp.py::test_completion  backend=daemon
```

Details worth knowing before you rely on them:

- **Values needn't be JSON-serializable.** Anything else is recorded as its
  `repr`. A probe must never be the thing that fails the test it was added to.
- **A repeated key collapses to its last value** in the summary and the record,
  so a probe inside a loop reads as "where did it get to". Every value is still
  in `notes.jsonl`.
- **Values over 2000 characters are clipped** in the summary, the record, and
  the log -- never in `notes.jsonl`. Something that big wants `attach()`.
- **Attachments are found by listing the directory**, not by remembering what
  `attach()` wrote, so a file a subprocess dropped in `agent_notes.dir` is
  listed just the same.
- **A note taken after the last test has finished** -- from another plugin's
  `pytest_sessionfinish`, an `atexit` handler, a background thread -- is in
  `notes.jsonl` but nowhere else: the summary has already been printed and
  there is no test's record left to put it in.
- **With agent mode off**, notes print (into pytest's own captured output) and
  attachments go under `<agent-dir>/attachments/`, since there is no run
  directory to record them in.

### Instead of `python -c`

A one-off `python -c '...'` to check what some function returns throws away
everything the test suite already has: fixtures, a configured environment,
imports that work, `profile` for timing it, and a place to put the output.
A throwaway test file costs the same to write and keeps all of it:

```python
def test_scratch_what_does_resolve_return(agent_notes, store):  # your own fixtures
    agent_notes.note(result=resolve(store, "0lqkw72fqnp7q1kr"))
```

`pytest tests/test_scratch.py` then prints the answer in its summary. Delete
the file when the question is answered.

### Options

| Flag | Env var | Default | Meaning |
| --- | --- | --- | --- |
| `--agent` | `PYTEST_AGENT` | auto-detected | Turn on agent mode |
| `--agent-dir` | `PYTEST_AGENT_DIR` | `.pytest-agent` | Where to write run detail (relative to rootdir) |
| `--agent-heartbeat` | `PYTEST_AGENT_HEARTBEAT` | `10` | Seconds between progress lines (0 prints none) |
| `--agent-stuck-after` | `PYTEST_AGENT_STUCK_AFTER` | `300` | Dump every thread's stack after one test has run this long (0 disables) |
| `--agent-status-interval` | `PYTEST_AGENT_STATUS_INTERVAL` | `2` | Seconds between rewrites of `status.json`, which names the test running now and its age. `pytest-agent watch` reads it (0 writes none) |
| `--agent-keep-runs` | `PYTEST_AGENT_KEEP_RUNS` | `20` | Keep only the newest N `runs-*` dirs (the just-finished run is never pruned); labeled runs get a second budget of the same size; `history.jsonl` entries are kept forever regardless |
| `--agent-max-summary-lines` | `PYTEST_AGENT_MAX_SUMMARY_LINES` | `40` | Terminal lines another plugin's end-of-run report (a `--cov` table) may take inline; past that it is pointed at rather than shown in part. 0 for no bound. Full text is in `reports.txt` regardless |
| `--agent-label` | `PYTEST_AGENT_LABEL` | *(none)* | Name this run, so later queries can find it by name instead of by number |
| `--agent-allow-pipe` | `PYTEST_AGENT_ALLOW_PIPE` | off | Skip the piped-stdout guard below |
| n/a | `PYTEST_AGENT_NO_AUTODETECT` | off | Disable the harness-env-var auto-activation |

### When a run hangs or gets killed

A run that never finishes is the one that most needs explaining, and it used
to be the only one that left nothing behind. Two things now cover it.

**A test that keeps running** gets its stack dumped where it stands. After
`--agent-stuck-after` seconds on one test (300 by default -- under the
`timeout 500 pytest` an agent typically uses, so the dump happens *before* the
kill), every thread's traceback is appended to `<test>.stuck.txt`, next to
where that test's `.log` will go, and one line names the file:

```
[pytest-agent] still running after 300s: tests/test_store.py::test_gc -- stack dumped to .pytest-agent/runs-0259/tests/test_store.py/test_gc.stuck.txt
```

It repeats up to five times per test and then stops: five stacks is enough to
tell a wedged test (identical every time) from a slow one (the stack moves).

**A killed run still reports.** SIGTERM is turned into the interrupt pytest
already handles gracefully, so `timeout 500 pytest tests` -- or any other
`kill` -- still ends with the usual summary block, still appends to
`history.jsonl`, and names what it died in:

```
[pytest-agent] done in 500.2s -- 284 passed, 1 failed, 0 error, 3 skipped, 0 collection errors
[pytest-agent] interrupted by SIGTERM while running: tests/test_store.py::test_gc
[pytest-agent]   its stack, dumped while it ran: .pytest-agent/runs-0259/tests/test_store.py/test_gc.stuck.txt
```

`summary.json` and `history.jsonl` carry the same as `interrupted_at` and
`killed_by`, so a killed run is greppable after the fact rather than being an
absence. A suite that installs its own SIGTERM handler keeps it -- pytest-agent
only takes over the default action.

The two halves complement each other on purpose. A signal handler can only run
when the interpreter gets a chance to run it, so a thread wedged inside a C
call never sees the SIGTERM at all -- but its stack was already written while
it hung. A second SIGTERM always kills the process outright, so an unresponsive
run stays killable.

#### Versus pytest's own `faulthandler_timeout`

pytest has a built-in for the same problem, and it is worth knowing which you
want. Setting the `faulthandler_timeout` ini option dumps every thread's stack
once per test after N seconds (`faulthandler_exit_on_timeout`, default off,
makes it kill the process too). It arms a C-level timer, so it fires even when
the main thread never releases the GIL -- a case `--agent-stuck-after`'s
Python watchdog thread cannot cover.

`--agent-stuck-after` writes to `<test>.stuck.txt` beside that test's log
rather than to raw stderr, repeats so you can distinguish a wedge from slow
progress, and names the file on the terminal. In agent mode stderr is
precisely the stream being kept quiet, and nothing on it reaches the per-test
record.

They compose: pytest-agent deliberately leaves `faulthandler`'s
process-global `dump_traceback_later` timer alone, so turning on
`faulthandler_timeout` does not disable either one. If all you want is "dump
and die", use pytest's and set `--agent-stuck-after=0`.

### Other plugins' reports are not swallowed

Agent mode replaces pytest's own per-test terminal output, which it does by
pointing the builtin terminal reporter at `terminal.txt` instead of the
terminal. Everything that reports *through* that writer would go with it --
so anything a plugin prints at the end of a run is picked back up and printed:

```sh
$ pytest --agent --cov=mypkg --cov-report=term-missing
[pytest-agent] done in 41.2s -- 311 passed, 0 failed, ...
[pytest-agent] full detail: /repo/.pytest-agent/runs-0262 (see index.jsonl)
[pytest-agent] also reported by other plugins (.pytest-agent/runs-0262/reports.txt):
================================ tests coverage ================================
... 87 more report lines not shown; full text in .pytest-agent/runs-0262/reports.txt (--agent-max-summary-lines=0 prints them here) ...
[pytest-agent] coverage: 91%
```

Three things are going on there:

- **The reports are saved as `reports.txt`** in the run directory, on their
  own rather than only as part of the `terminal.txt` transcript, so the pointer
  leads to a file that needs no searching.
- **A report longer than `--agent-max-summary-lines` (40) is not printed in
  part**, only pointed at -- its own first line survives so you can tell which
  report is waiting. Half a coverage table is worse than a pointer to all of
  it, because it reads as all of it. Short reports (`--durations`,
  `--junit-xml`'s path, a small table) print inline as before, and `0` prints
  everything inline.
- **A coverage percentage gets a prefixed line of its own.** It is the number
  the run was for, and getting it off the last row of a table means reading the
  table.

This covers `--durations`, `--junit-xml`'s "generated xml file" line, and any
third-party plugin's `pytest_terminal_summary` -- pytest-agent needs no
knowledge of them. The percentage is the one exception, and even that is
scraped from the report text rather than read off pytest-cov's plugin object,
so there is no dependency and no private attribute to go stale.

The bound is settable as `PYTEST_AGENT_MAX_SUMMARY_LINES` too, and `0` turns it
off. Raising it is the right move if you want a table inline; it exists to stop
a long report burying the failure list above it, not to keep anything from you.

Nothing pytest itself would have printed is destroyed either; it is all in
`terminal.txt`, which is the file to read when a problem is about pytest's own
reporting rather than about a test.

### Profiling a slow test

Add `profile` as a test parameter to profile it with
[pyinstrument](https://github.com/joerick/pyinstrument) and get a text report
written to disk automatically -- no other change to the test body:

```python
def test_something_slow(profile):
    do_the_slow_thing()
```

The report lands next to that test's other output -- `test_bar.profile.txt`
alongside `test_bar.log`/`test_bar.json` under the current run directory when
`--agent` is active, or under a fixed `<agent-dir>/profiles/...` (overwritten
each run) otherwise.

`pyinstrument` is a hard dependency of pytest-agent itself, not an optional
extra -- pytest-agent is the thing that's optional (only pulled into a
project's environment, here via `nix/shell.nix`, when its detail-on-disk
philosophy is wanted at all), so there's no lighter-weight "pytest-agent
without a profiler" install worth supporting.

### The piped-stdout guard

Independently of `--agent` -- and independently of
`PYTEST_AGENT_NO_AUTODETECT`, which only governs auto-activation -- this
plugin refuses to run at all (exit code 2, before collecting a single test)
if it detects its own stdout is piped directly into `head`, `tail`, `cut`,
`sed`, `awk`, or any grep -- GNU's, plus `ugrep`, `rg`, `ag`, `ack`,
`pcregrep` and friends -- exactly the mistake this project exists to prevent,
since those tools silently discard the output that would explain a failure.

The whole grep family is listed rather than just `grep`, because the guard's
audience is an agent, and an agent told to stop using `grep` reaches for `rg`.
`wc` is deliberately absent, for the same reason `tee` is unaffected: asking
for a count destroys the output wholesale rather than reading it through a
keyhole, which is an explicit choice rather than a mistake. (`tee` is also how
you'd capture full output to a file while still watching it live.)

The reader is identified by three things -- argv[0], `comm`, and
`/proc/<pid>/exe` -- not by any one of them. A wrapper makes those disagree:
Claude Code replaces `grep` with a shell function running
`exec -a ugrep "$CLAUDE_CODE_EXECPATH"`, so a pipe an agent wrote as
`| grep ...` appears in /proc as `comm=.claude-wrapped`, `argv[0]=ugrep`.
Matching on `comm` alone missed it, which meant the guard was blind to
precisely the caller it exists to stop. Later arguments are deliberately not
scanned: `| tee grep.log` is not a violation, and a guard that refuses honest
commands is one people switch off.

Only the *immediate* reader is inspected, so `pytest | cat | grep x` is not
caught. Following the chain conflicts with the `tee` allowance above, so this
is a known limitation rather than an oversight (see TODO.md).

Runs that only print a listing and execute no test body are exempt, because
there is no failure detail for a pipe to discard: `--collect-only`,
`--fixtures`, `--fixtures-per-test`, `--markers`, `--setup-plan`, `--help`,
and `--version`. So `pytest --collect-only -q | tail -1` -- the normal way to
ask how many tests a selection matches -- just works. `--setup-only` is
deliberately not exempt: it really does execute fixtures, and a fixture error
there is exactly what the guard protects.

`--agent-allow-pipe` (or `PYTEST_AGENT_ALLOW_PIPE=1`) skips the guard. It is
intentionally not mentioned in the refusal message: an agent reading that
refusal should stop truncating, not learn a flag that lets it keep
truncating.

### pytest-xdist

`pytest -n N` works with agent mode: one run directory, one `index.jsonl`, one
`history.jsonl` entry, and the queries answer about the whole run.

Recording happens in the controller, because it is the one process that sees
everything. xdist ships each worker's `TestReport` back, and a serialized
report carries the traceback, the captured stdout/stderr/logging sections and
the durations -- which is everything the recorder reads. Workers detect
themselves by the `workerinput` attribute xdist sets on their config and record
nothing, so no worker claims a second `runs-NNNN`. (That scattering, with every
query silently answering from whichever worker won the race for the highest
number, is what made these two incompatible before.)

Two things degrade, and the startup banner says so rather than leaving it to be
discovered:

- **`note()` and `attach()`** run inside a worker, where no runtime is
  registered, so they fall back to printing. That print lands in the worker's
  captured output, which *is* shipped -- so a note still reaches the test's
  `.log`, but not `notes.jsonl` and not the `notes` field in `index.jsonl`.
- **Stuck-test dumps are off.** A dump is `faulthandler` dumping the calling
  process's threads; from the controller that would show the controller idling
  and name whichever test happened to be current, which reads like evidence
  while being none. `--agent-stuck-after` is forced to 0 under `-n`.

Both are the same missing piece -- worker-side recording -- and both are
recoverable by running without `-n` when the detail matters more than the wall
clock.

One caveat that is xdist's rather than agent mode's: with `-n`, several tests
are in flight at once, so the progress line's `cur=` names one of them rather
than the only one.

`--agent-allow-pipe` (or `PYTEST_AGENT_ALLOW_PIPE=1`) skips the guard. It is
intentionally not mentioned in the refusal message: an agent reading that
refusal should stop truncating, not learn a flag that lets it keep
truncating.

## Development

No install needed against the ambient nix-provided Python; `pyproject.toml`
sets `pythonpath = ["src"]` for pytest-agent's own tests. From this
directory:

```sh
pytest
```

The suite is recursive: most of what pytest-agent does only exists inside a
real pytest session, so the tests run *inner* pytest sessions (via
`pytest.Pytester`) and inner CLI invocations against throwaway project
directories, then read back what those wrote. `tests/test_agent_workflow.py`
is the end-to-end one -- it fails a small suite the way the debugging session
that motivated these features did (parametrized tests sharing one root cause,
plus an unrelated failure) and then drives `digest`/`show`/`last-failures`
over the result, including feeding the printed paths back to a shell to prove
they're readable verbatim.

There's no bootstrapping escape hatch to remember, and no env var to disable
the plugin for it. Two things in `tests/conftest.py` handle it:
`_clean_agent_env` (autouse) clears every harness and `PYTEST_AGENT_*`
variable so inner runs start from a known-empty configuration -- this repo's
dev environment genuinely sets `CLAUDECODE`, which would otherwise make every
inner run auto-activate and several tests pass for the wrong reason -- and
`run_cli()` runs the CLI as a real subprocess in a given directory. The pipe
guard needs nothing special: it inspects the *inner* process's own stdout,
which Pytester points at a file.
