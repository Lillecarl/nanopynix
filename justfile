# pynixd justfile

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
    pytest tests -v --timeout=60 -m "not slow" --durations 50

# Run all checks
precommit: fmt test
