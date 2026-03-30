# pynixd justfile

default:
    @just --list

# Run type checker
lint:
    pyright pynixd
    ty check pynixd

# Run linter/formatter check
check:
    ruff check pynixd
    ruff format --check pynixd

# Format code
fmt:
    ruff check --fix pynixd
    ruff format pynixd

# Run tests
test:
    pytest tests -v --timeout=60

# Run all checks
precommit: fmt lint test
