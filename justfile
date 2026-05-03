default:
    @just --list

check:
    pyright pynixd
    pyright tests/conftest.py tests/functional tests/unit

fmt:
    ruff format pynixd
    ruff check --fix pynixd
    ruff format tests
    ruff check --fix tests

upgrade:
    pyupgrade --py313-plus

proper: check fmt upgrade

# Run tests
test:
    pytest tests/functional tests/unit

# aitest: check
#     #!/usr/bin/env bash
#     logfile=$(mktemp)
#     echo "Logfile: $logfile"
#     pytest tests -v --timeout=60 --timeout-method=thread -m "not slow and not bench" --durations=50 --maxfail=0 2>&1 | tee $logfile
#     echo "Logfile: $logfile"

# Run all checks
precommit: check fmt test

# Run Lix test matrix (all 8 combinations)
supermegatest:
    pytest tests/functional --client-bin=nix --local-bin=nix --builder-bin=nix
    pytest tests/functional --client-bin=nix --local-bin=nix --builder-bin=lix
    pytest tests/functional --client-bin=nix --local-bin=lix --builder-bin=nix
    pytest tests/functional --client-bin=nix --local-bin=lix --builder-bin=lix
    pytest tests/functional --client-bin=lix --local-bin=nix --builder-bin=nix
    pytest tests/functional --client-bin=lix --local-bin=nix --builder-bin=lix
    pytest tests/functional --client-bin=lix --local-bin=lix --builder-bin=nix
    pytest tests/functional --client-bin=lix --local-bin=lix --builder-bin=lix
