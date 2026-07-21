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

The CLI prints a directory path at the start and end of the run and, every
`--agent-heartbeat` seconds while tests are running, one line like:

```
[pytest-agent] 42s | 118 passed, 2 failed, 120/311 total | running: tests/test_foo.py::test_bar
```

That's it -- nothing else prints. Whether something is progressing or stuck
is visible from that line alone: the counts and the running test change
between prints if things are moving, and elapsed keeps climbing with nothing
else changing if they aren't. Everything else (per-test stdout/stderr/log/
tracebacks, an index, a run summary) is written under that directory
(`.pytest-agent` by default) for you or an agent to read directly:

```
.pytest-agent/
  index.jsonl              # one JSON record per test, appended as it finishes
  summary.json             # exit status, duration, counts, written at the end
  collect_errors/          # one log per module that failed to import/collect
  tests/
    tests/test_foo.py/
      test_bar.log          # nodeid, outcome, duration, traceback, captured
                             # stdout/stderr/log, one file per phase section
      test_bar.json          # the same record that's in index.jsonl
```

Find failures without piping anything:

```sh
jq -c 'select(.outcome == "failed" or .outcome == "error")' .pytest-agent/index.jsonl
cat .pytest-agent/tests/tests/test_foo.py/test_bar.log
```

### Options

| Flag | Env var | Default | Meaning |
| --- | --- | --- | --- |
| `--agent` | `PYTEST_AGENT` | off | Turn on agent mode |
| `--agent-dir` | `PYTEST_AGENT_DIR` | `.pytest-agent` | Where to write run detail (relative to rootdir) |
| `--agent-heartbeat` | `PYTEST_AGENT_HEARTBEAT` | `10` | Seconds between progress lines |
| `--agent-allow-pipe` | `PYTEST_AGENT_ALLOW_PIPE` | off | Skip the piped-stdout guard below |

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
