# YAML primops

Built-in `builtins.fromYAML`/`builtins.toYAML`-style primops, registered by
passing `yaml_primops()` to `Session(primops=...)`. See
{doc}`../examples` for a runnable walkthrough, including how to register a
custom Python-backed primop alongside these.

```{note}
Primop registration — these YAML primops and custom
`Session(primops=..., primop_callables=...)` registration alike — requires
Nix >= 2.32. Every supported Nix meets that.
```

```{eval-rst}
.. automodule:: nanopynix.primops
   :members:

.. automodule:: nanopynix.primops.yaml
   :members:
```
