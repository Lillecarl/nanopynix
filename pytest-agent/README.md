# pytest-agent

A pytest plugin that makes pytest runs friendly to an AI agent driving them
from a terminal: minimal, low-noise CLI output, and everything an agent could
need written to disk instead, split per test file and test name.

This lives inside the nanopynix repo for now but has no dependency on it; it
may get extracted into its own repo later.

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
    index.jsonl              # one JSON record per test, appended as it finishes;
                             # failures also carry `crash` (exception type,
                             # message, file:line) and `frames` (traceback
                             # locations, each tagged first-party or not)
    summary.json              # the same fields as this run's history.jsonl line
    collect_errors/          # one log per module that failed to import/collect
    tests/test_foo.py/
      test_bar.log            # nodeid, outcome, duration, traceback, captured
                               # stdout/stderr/log, one file per phase section
      test_bar.json            # the same record that's in index.jsonl
  runs-0002/
    ...
```

pytest-agent always prints the exact run directory in its startup and final
banner lines (`run 2: writing full per-test detail to: ...`) -- that's the
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
| `pytest-agent help` | The above, with flags |

All of them take `--run N` to read an older run instead of the newest, and
`--dir PATH` to point at an agent directory other than the nearest
`.pytest-agent` at or above the current directory.

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

The first argument decides: `show`, `last-failures`, `digest`, and `help` are
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

### Options

| Flag | Env var | Default | Meaning |
| --- | --- | --- | --- |
| `--agent` | `PYTEST_AGENT` | auto-detected | Turn on agent mode |
| `--agent-dir` | `PYTEST_AGENT_DIR` | `.pytest-agent` | Where to write run detail (relative to rootdir) |
| `--agent-heartbeat` | `PYTEST_AGENT_HEARTBEAT` | `10` | Seconds between progress lines |
| `--agent-keep-runs` | `PYTEST_AGENT_KEEP_RUNS` | `20` | Keep only the newest N `runs-*` dirs (the just-finished run is never pruned); `history.jsonl` entries are kept forever regardless |
| `--agent-allow-pipe` | `PYTEST_AGENT_ALLOW_PIPE` | off | Skip the piped-stdout guard below |
| n/a | `PYTEST_AGENT_NO_AUTODETECT` | off | Disable the harness-env-var auto-activation |

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
if it detects its own stdout is piped directly into `head`, `tail`, `grep`,
`sed`, `awk`, or a close variant -- exactly the mistake this project exists
to prevent, since those tools silently discard the output that would explain
a failure. Piping into `tee` is unaffected (and is how you'd capture full
output to a file while still watching it live).

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
