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
test: check
    pytest tests -v --timeout=60 --timeout-method=thread -m "not slow" --durations 50

aitest: check
    #!/usr/bin/env bash
    logfile=$(mktemp)
    echo "Logfile: $logfile"
    pytest tests -v --timeout=60 --timeout-method=thread -m "not slow" --durations=50 --maxfail=0 2>&1 | tee $logfile
    echo "Logfile: $logfile"

# Run all checks
precommit: check test
