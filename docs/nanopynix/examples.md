# Examples

Every example on this page lives under `docs/examples/` in the repository
and is executed as part of the test suite
(`tests/nanopynix/test_examples.py`). If the API changes in a way that
breaks one of these, CI fails — the examples can't silently go stale.

## Evaluating Nix expressions

Navigating attrsets and lists lazily, forcing scalars, and converting whole
value trees to Python/JSON.

```{literalinclude} ../examples/eval_example.py
:language: python
```

## Evaluating a flake

Locking and evaluating a flake, then navigating its outputs.

```{literalinclude} ../examples/flake_example.py
:language: python
```

## Querying the Nix store

Path metadata, closures, and GC roots — all read-only store operations.

```{literalinclude} ../examples/store_example.py
:language: python
```

## Nix settings

Building `NixSettings`, rendering them as `nix.conf`, and comparing against
Nix's live setting registry.

```{literalinclude} ../examples/settings_example.py
:language: python
```

## Custom primops and YAML

Registering a Python callable as a Nix builtin over the worker's RPC
backchannel, plus the built-in YAML primops.

```{note}
Requires dynamic primop registration, which every supported Nix has.
```

```{literalinclude} ../examples/primops_example.py
:language: python
```

## In-process (L2) API

The same store queries and eval navigation as above, but through
`nanopynix.inproc` instead of a worker subprocess. See
{doc}`architecture` for how the two compare.

```{literalinclude} ../examples/inproc_example.py
:language: python
```
