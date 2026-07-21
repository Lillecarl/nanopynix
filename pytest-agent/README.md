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

That's it -- nothing else prints. Whether something is progressing or stuck
is visible from that line alone: the counts and `cur` change between prints
if things are moving, and elapsed keeps climbing with nothing else changing
if they aren't. Everything else (per-test stdout/stderr/log/tracebacks, an
index, a run summary) is written to disk for you or an agent to read
directly. Each invocation gets its own numbered run directory under
`--agent-dir` (`.pytest-agent` by default), so nothing from a previous run is
ever overwritten:

```
.pytest-agent/
  history.jsonl             # one line per run, appended when it finishes --
                             # duration, counts, hostname, git rev, etc.
  runs-0001/
    index.jsonl              # one JSON record per test, appended as it finishes
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

Find failures without piping anything:

```sh
jq -c 'select(.outcome == "failed" or .outcome == "error")' .pytest-agent/runs-0002/index.jsonl
cat .pytest-agent/runs-0002/tests/test_foo.py/test_bar.log
```

Track a test's duration across runs:

```sh
jq -c 'select(.nodeid == "tests/test_foo.py::test_bar") | .duration_s' .pytest-agent/runs-*/index.jsonl
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

### The piped-stdout guard

Independently of `--agent`, this plugin refuses to run at all (exit code 2,
before collecting a single test) if it detects its own stdout is piped
directly into `head`, `tail`, `grep`, `sed`, `awk`, or a close variant --
exactly the mistake this project exists to prevent, since those tools
silently discard the output that would explain a failure. Piping into `tee`
is unaffected (and is how you'd capture full output to a file while still
watching it live). Pass `--agent-allow-pipe` if a piped run is genuinely what
you want.

## Development

No install needed against the ambient nix-provided Python; `pyproject.toml`
sets `pythonpath = ["src"]` for pytest-agent's own tests. From this
directory:

```sh
pytest
```
