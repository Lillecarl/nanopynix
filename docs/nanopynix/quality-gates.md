# Quality gates

This page gives the exact command for each gate, and says when each gate runs.

`#22` tracks the work to make the static gates run in CI. Until that issue
closes, a person runs them by hand.

## Every commit — fast, hermetic, no Nix daemon needed

```
direnv exec . ruff format --check
direnv exec . ruff check
direnv exec . ruff check --config ruff-strict.toml
direnv exec . pyright
direnv exec . pytest tests/nanopynix/test_settings.py tests/nanopynix/test_stores.py \
                     tests/nanopynix/test_models.py tests/nanopynix/test_exceptions_classify.py \
                     tests/nanopynix/test_protocols.py tests/nanopynix/test_engine_parity.py
```

Policy: all four static gates report zero. `ruff-strict.toml` stays at zero
findings; a new finding comes from the change under review. Never pass
`--unsafe-fixes`. Never run `treefmt` as a check — it writes.

## Every pull request — the correctness gate

```
direnv exec . timeout 1500 pytest tests --nix-test-backends local,daemon
```

Policy: no failures, no new skips. A skip that is new must name the capability
it needs, through an existing marker (`nix_version`, `nix_capability`,
`nix_known_issue`).

## Every pull request — the drift gate (new)

```
nix build --file . checks.drift
```

Runs `check_all_settings_model_drift(include_optional=True)` and
`check_all_store_model_drift()` under each supported Nix. Policy: `missing` and
`extra` are both empty for every surface and every store model. This is what
catches a Nix release adding or renaming a setting.

## Merge to the default branch — the expensive matrix

* `--nix-test-backends local,daemon` under Nix 2.34 and 2.35.
* The ThreadSanitizer jobs: the `concurrency` marker, and the `soak`, which
  runs every eligible test in overlapping lanes under five seeds. A seed fixes
  which tests overlap, so a race that a job finds can be run again.
* Coverage, with the forkserver instrumentation already configured in
  `pyproject.toml`.

Policy on coverage: use it to find untested *modules*, not to hold a
percentage. The useful boundary here is per-file: a file under 60 % is worth a
look, and the whole-project number is not worth a gate.

## Platform-specific, opt-in

* The namespace tests, which need unprivileged user namespaces and a
  filesystem with user extended attributes. `probe_namespace_support` already
  reports this; keep the skip keyed to it.
* The `benchmark` marker stays opt-in and is never a correctness gate.

## What deliberately stays out

* A second type checker. `pyright --strict` plus beartype at runtime is
  sufficient coverage of the same question, and mypy would need its own
  suppression vocabulary in 142 places.
* A separate import linter. The dependency rules in
  `architecture-principles.md` are three lines, and they are better as a test
  than as a tool.
* Any tool that writes in CI.

