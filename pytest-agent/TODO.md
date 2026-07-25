# pytest-agent TODO

Friction observed while an agent used pytest-agent heavily for a debugging /
audit session (CIP3 error-pipeline work). Each item is something the agent
either worked around or spent turns on that the tool could have just handed it.

## 1. Pipe guard fires on `--collect-only`, where there is no detail to lose

`pytest --collect-only -q | tail` was refused by the pipe guard. The guard's
rationale — "truncating hides the real failure" — does not apply to
`--collect-only`: there are no failures, and the interesting output (the
collected count) is deliberately at the *end*.

Workaround used: redirect to a file, then `tail` the file. That is pure
ceremony; the guard was satisfied while the agent read exactly the same
truncated view.

Suggestions:
- Skip the pipe guard entirely when `--collect-only` is active.
- Or provide `pytest-agent count [paths...]` for "how many tests does this
  select", which is the actual question being asked.

Related surprise: `PYTEST_AGENT_NO_AUTODETECT=1` suppresses auto-activation
but the pipe guard *still* fired. If the guard is intentionally independent of
activation, say so in the refusal message, because the agent's next move was
to assume the env var had failed to take effect.

## 2. No addressable way to read one test's detail

To read one failure the agent had to hand-assemble:

    .pytest-agent/runs-0259/tests/pynix/test_lsp_scenarios.py/test_hover_on_a_kind_name_summarizes_it_via_the_openapi_schema[in_process-local].log

That requires knowing the run number, mirroring the test file path, and shell
quoting `[`/`]` in parametrized IDs. Every one of those is a chance to get it
wrong, and getting it wrong costs a whole turn.

Suggestions:
- `pytest-agent show '<nodeid>'` — prints that test's detail from the latest
  run (or `--run N`). Accept a unique substring of the nodeid, not just an
  exact match.
- `pytest-agent last-failures [--detail]` — the failing nodeids, optionally
  with each one's full detail inlined.

## 3. Print the resolved log path next to each failed test

The end-of-run summary lists failing nodeids and, separately, the run
directory. Printing each failure's resolved `.log` path on its own line would
eliminate item 2's path construction for the common case, with no new
subcommand:

    [pytest-agent]   tests/pynix/test_lsp_scenarios.py::test_hover_...[in_process-local]
    [pytest-agent]     -> .pytest-agent/runs-0259/tests/pynix/test_lsp_scenarios.py/test_hover_...[in_process-local].log

## 4. Wanted a "just the cause" digest across failures

With 18 failures sharing one root cause, the agent wanted the exception line
per test and nothing else, and resorted to `grep -n "Error|assert" <log>` on
individual files. A digest that, per failed test, prints the exception line
plus the traceback frames *in first-party code* (filtering `site-packages` and
stdlib) would have answered "are these all the same bug?" in one command.
Here that would have immediately shown all 18 ending in
`FileNotFoundError: /nix/store/...-swagger.json`.

Suggestion: `pytest-agent digest [--run N]`, grouping failures by normalized
exception type + message so shared root causes collapse into one entry with a
count.
