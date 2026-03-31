# pynixd justfile

default:
    @just --list

check: fmt
    pyright pynixd

fmt:
    ruff check --fix pynixd
    ruff format pynixd

# Run tests
test:
    pytest tests -v --timeout=60

# Run all checks
precommit: fmt test
