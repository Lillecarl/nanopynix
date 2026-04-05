default:
    @just --list

check: fmt
    pyright pynixd
    pyright tests

fmt:
    ruff check --fix pynixd
    ruff format pynixd
    ruff check --fix tests
    ruff format tests

# Run tests
test:
    # pytest tests -v --timeout=60 --timeout-method=thread -m "not slow and not bench" --durations 50
    pytest tests -m "not slow and not bench"

aitest: check
    #!/usr/bin/env bash
    logfile=$(mktemp)
    echo "Logfile: $logfile"
    pytest tests -v --timeout=60 --timeout-method=thread -m "not slow and not bench" --durations=50 --maxfail=0 2>&1 | tee $logfile
    echo "Logfile: $logfile"

# Run all checks
precommit: check test
