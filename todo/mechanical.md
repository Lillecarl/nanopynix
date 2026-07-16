# Mechanical cleanup

## How to use this file

Each section below is a self-contained prompt for a mechanical agent. Give the
agent exactly one section at a time. The agent must make only the described
mechanical changes, run the stated validation, and report any ambiguity rather
than designing an API or changing behaviour. Remove the completed section from
this file in the same change. Do not combine sections into one large cleanup.

## Prompt: Apply deferred Ruff-strict cleanup

Run:

```console
direnv exec . ruff check --config ruff-strict.toml python/src tests
```

Apply only safe, semantics-preserving fixes required by that command in
`python/src` and `tests`. In particular, fix deferred imports, quoted
annotations in `cast(...)` calls, and sorted `__all__` entries if they are
reported. Do not change public API, test assertions, runtime behaviour, or
Ruff configuration. Re-run the exact command until it passes, then remove this
prompt from `todo/mechanical.md` and report the files changed.
