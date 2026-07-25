---
name: pytest-agent
description: How to run pytest and read its results when the pytest-agent plugin is installed. Use when running tests, investigating a test failure, deciding whether a failure is new, re-running failures, profiling a slow test, or diagnosing a hung or killed run.
---

# pytest-agent

pytest-agent turns a pytest run into two separate things: a terminal that stays
quiet, and a full record on disk that you query afterwards. If it is installed,
it is already on — it auto-activates when a coding-agent harness env var
(`CLAUDECODE`, `CURSOR_AGENT`, `GEMINI_CLI`, `CODEX_SANDBOX`, `AI_AGENT`, ...)
is present, with no flag needed.

The consequence for you: **the terminal is not where the answer is.** A run
prints a progress line every 10s and one path per failure. Everything else —
tracebacks, captured stdout/stderr/logging, per-test records — is on disk, and
there are commands that read it.

## The one hard rule

**Never pipe pytest's stdout into `head`, `tail`, `grep`, `sed`, `awk`, or a
variant.** The plugin detects this and refuses to run: exit code 2, before a
single test is collected. This guard is independent of agent mode and is *not*
turned off by `PYTEST_AGENT_NO_AUTODETECT=1`.

There is nothing to work around here. Those pipes exist to cut output down,
and the output is already cut down — piping it only discards the one line
naming the failure you were looking for. Run it unpiped and query afterwards.

Piping into `tee` is fine. Listing-only runs are exempt (`--collect-only`,
`--fixtures`, `--fixtures-per-test`, `--markers`, `--setup-plan`, `--help`,
`--version`), so `pytest --collect-only -q | tail -1` works. `--setup-only` is
not exempt — it really executes fixtures.

The query subcommands below are ordinary read-only programs. Grepping *those*
is fine.

## The loop

```sh
pytest tests/                    # unpiped; detail goes to .pytest-agent/runs-NNNN/
pytest-agent digest              # what broke, grouped by root cause
pytest-agent show '<nodeid>'     # one failure, in full
# ...fix...
pytest-agent rerun               # just the failures, not the suite
pytest-agent compare             # what the fix actually changed
```

Start with `digest`, not `last-failures`: 18 failures with one cause read as
one entry, which tells you immediately whether you are looking at one bug or
eighteen.

## Commands

| Command | Answers |
| --- | --- |
| `pytest-agent digest` | Failures grouped by root cause, each with a count and the first-party traceback frames |
| `pytest-agent last-failures` | Every failing test, each with the path to its log |
| `pytest-agent last-failures --detail` | ...with every failure's full log inlined |
| `pytest-agent show '<pattern>'` | One test's full detail. `<pattern>` is a nodeid or any substring matching exactly one; `-a` prints all matches instead of refusing an ambiguous one |
| `pytest-agent history '<pattern>'` | That test's outcome in every run still on disk — did I break this, or was it already failing? `--limit N` for the newest N runs |
| `pytest-agent compare [OLD NEW]` | Newly failing / newly passing / still failing between two runs (default: the two newest) |
| `pytest-agent rerun [pytest args...]` | Re-run exactly the recorded failures. The only subcommand that starts a pytest session |
| `pytest-agent help` | The above, with flags |

`show`, `last-failures`, `digest` and `rerun` take `--run N|LABEL` to read a
run other than the newest. `history` and `compare` read across runs instead —
`history` has no `--run`, and `compare` names its two runs positionally (either
order). All take `--dir PATH`; by default they find the nearest
`.pytest-agent` at or above the cwd.

Anything that is not one of those words is forwarded to pytest with `--agent`:
`pytest-agent -x tests/` is `pytest --agent -x tests/`. `python -m pytest_agent
...` is the same CLI when the console script is not on `PATH`.

### `rerun` vs pytest's `--lf`

`--lf` needs no plugin and handles the common case. Reach for `rerun` when it
cannot: pytest's cache holds only the last run per rootdir, so the first
`-k`-filtered or `-x` re-run overwrites the list you were working through —
whereas every run on disk keeps its own, addressable by `--run N|LABEL`. Ids
also go straight into pytest's argv, so nothing has to quote
`test_hover[in_process-local]`.

## Where things are

Each invocation gets its own `runs-NNNN/`, so nothing is ever overwritten. The
run directory is printed in the startup and final banner lines. To find the
newest without having seen a banner:

```sh
tail -n1 .pytest-agent/history.jsonl | jq -r .run_dir
```

There is deliberately no "latest" symlink — under concurrent runs, anything
that swaps the same path is a race.

```
.pytest-agent/
  history.jsonl               # one line per run, ever; never pruned
  runs-0002/
    index.jsonl               # one record per test: outcome, duration, notes,
                              # and for failures `crash` + `frames`
    summary.json              # this run's counts
    meta.json                 # written *before* the first test: run number,
                              # label, pid, argv -- so an in-progress run is
                              # already queryable
    terminal.txt              # what plain pytest would have printed, verbatim
    notes.jsonl               # one line per note() call
    collect_errors/           # one log per module that failed to import
    tests/test_foo.py/
      test_bar.log            # the file to read: traceback + captured output
      test_bar.json           # the same record as in index.jsonl
      test_bar.files/         # whatever the test attached
      test_bar.stuck.txt      # thread stacks, if it ran long enough
```

The failure lines printed at the end are paths, with `::` written as a
directory separator, resolved and shell-quoted so they can be read back
directly. A nodeid is appended in parentheses only when the path lost
something (a `/` in a parametrized id becomes `_` on disk) — when it is there,
it is the addressable form, and it feeds straight back into `pytest-agent
show`.

Reading the files directly is a first-class option:

```sh
jq -c 'select(.outcome == "failed" or .outcome == "error")' .pytest-agent/runs-0002/index.jsonl
jq -c 'select(.nodeid == "tests/test_foo.py::test_bar") | .duration_s' .pytest-agent/runs-*/index.jsonl
```

## Long runs in the background

Name a slow suite so you can find it again without knowing how many runs
happened while it went:

```sh
pytest --agent-label full-suite tests/     # 10 minutes, in the background
pytest tests/test_parser.py                # meanwhile, focused runs
pytest-agent last-failures --run full-suite
pytest-agent rerun --run full-suite
```

- The label works **while the run is still going** — querying an unfinished
  run answers from what it has recorded so far and says so on stderr.
- Labeled runs get their own retention budget, so focused runs cannot evict
  the labeled one.
- A label is never all-digits (that would be a run number); letters, digits,
  `.`, `_`, `-`, up to 64 characters. Labels need not be unique — the newest
  match wins.
- `PYTEST_AGENT_LABEL` does the same for a whole shell or CI job.

## Getting values out of a test

A `print()` already lands in that test's log. Notes do what it cannot: be
queried across a run, hold something too big for a line, or come from deep
inside the code under test.

```python
def test_resolution(agent_notes):
    agent_notes.note(store=store_path, backend=backend)   # structured, queryable
    agent_notes.attach("payload.json", raw_response)      # too big for a line
    (agent_notes.dir / "dump.bin").write_bytes(blob)      # or write files yourself
```

```python
from pytest_agent import note   # no fixture; callable from anywhere at all

def resolve(digest):            # ...including inside the code under test
    note(resolving=digest)
```

A note lands in the end-of-run summary (so you read it in the same turn that
ran the test), in `notes.jsonl`, in `index.jsonl` as `notes`, and in the test's
`.log` above the traceback. Values need not be JSON-serializable (anything else
is recorded as its `repr` — a probe must never fail the test it was added to);
a repeated key collapses to its last value everywhere except `notes.jsonl`; and
values over 2000 characters are clipped everywhere except `notes.jsonl`.

### Prefer a throwaway test to `python -c`

A one-off `python -c '...'` throws away the fixtures, the configured
environment, and the imports that already work. A scratch test file costs the
same to write and keeps all of it:

```python
def test_scratch_what_does_resolve_return(agent_notes, store):
    agent_notes.note(result=resolve(store, "0lqkw72fqnp7q1kr"))
```

`pytest tests/test_scratch.py` prints the answer in its summary. Delete the
file when the question is answered.

### Profiling

Add `profile` as a test parameter — no other change to the body:

```python
def test_something_slow(profile):
    do_the_slow_thing()
```

A pyinstrument text report lands at `test_something_slow.profile.txt` beside
that test's other output.

## When a run hangs or is killed

Both cases leave evidence; look for it before re-running.

- **A test that keeps running** gets every thread's stack appended to
  `<test>.stuck.txt` after `--agent-stuck-after` seconds (300 by default,
  chosen to fire *before* the `timeout 500 pytest` an agent typically uses),
  and the path is printed. It repeats up to five times per test — identical
  stacks mean wedged, moving stacks mean slow.
- **A killed run still reports.** SIGTERM becomes the interrupt pytest handles
  gracefully, so the run still prints its summary, still appends to
  `history.jsonl`, and names what it died in. `summary.json` and
  `history.jsonl` carry `interrupted_at` and `killed_by`.

So after a `timeout ... pytest` that was killed: read the summary block, then
the `.stuck.txt` it names. A second SIGTERM always kills outright, so an
unresponsive run stays killable.

If pytest's own `faulthandler_timeout` ini option is set, it keeps working —
pytest-agent leaves that timer alone. It fires even when the main thread never
releases the GIL, which the Python watchdog cannot cover; use it (with
`--agent-stuck-after=0`) if all you want is "dump and die".

## Options

| Flag | Env var | Default | Meaning |
| --- | --- | --- | --- |
| `--agent` | `PYTEST_AGENT` | auto-detected | Turn on agent mode |
| `--agent-dir` | `PYTEST_AGENT_DIR` | `.pytest-agent` | Where run detail goes (relative to rootdir) |
| `--agent-label` | `PYTEST_AGENT_LABEL` | *(none)* | Name this run for later queries |
| `--agent-heartbeat` | `PYTEST_AGENT_HEARTBEAT` | `10` | Seconds between progress lines (0 prints none) |
| `--agent-stuck-after` | `PYTEST_AGENT_STUCK_AFTER` | `300` | Dump thread stacks after one test runs this long (0 disables) |
| `--agent-keep-runs` | `PYTEST_AGENT_KEEP_RUNS` | `20` | Keep the newest N `runs-*` dirs; labeled runs get a second budget of the same size; `history.jsonl` is kept forever |
| `--agent-allow-pipe` | `PYTEST_AGENT_ALLOW_PIPE` | off | Skip the piped-stdout guard |
| n/a | `PYTEST_AGENT_NO_AUTODETECT` | off | Disable harness auto-activation |

## Things that will otherwise surprise you

- **Other plugins' end-of-run reports are not lost.** Agent mode redirects
  pytest's builtin reporter to `terminal.txt`, then reprints what any plugin
  wrote at the end — coverage tables, `--durations`, `--junit-xml`'s path.
  Long reports are elided in the middle; the full text is in `terminal.txt`.
- **`terminal.txt` is the file to read** when the problem is with pytest's own
  reporting rather than with a test.
- **`--agent-keep-runs` prunes old run directories**, so `history` and
  `compare` say how many runs they actually read. "Failed in 2 of 3 runs" means
  three runs *still on disk*, not three runs ever.
- **pytest-xdist (`-n auto`) does not work with agent mode.** Every worker
  process runs `pytest_configure` and claims its own `runs-NNNN`, so one
  logical run scatters across N partial records and queries silently answer
  from whichever worker won. Run without `-n` for the detail, or set
  `PYTEST_AGENT_NO_AUTODETECT=1` for xdist's speed.
- **A run that cannot write its directory turns agent mode off** rather than
  failing the run, and says so loudly on stderr. If you see that, the next
  query will answer from a *stale* run — fix `--agent-dir` before believing it.
