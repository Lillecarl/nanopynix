# Mechanical cleanup

- Run `ruff check --config ruff-strict.toml python/src tests` and apply the
  strict-only mechanical fixes. Current findings include deferred-import and
  quoted-`cast` annotations in the new local-runtime/protocol tests, plus
  sorted `__all__` entries.
