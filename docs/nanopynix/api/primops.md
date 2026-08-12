# YAML primops

Built-in `builtins.fromYAML`/`builtins.toYAML`-style primops, registered by
passing `yaml_primops()` to `Session(primops=...)`. See
{doc}`../examples` for a runnable walkthrough, including how to register a
custom Python-backed primop alongside these.

```{note}
Primop registration — these YAML primops and custom
`Session(primops=..., primop_callables=...)` registration alike — requires
Nix >= 2.32. Every version nanopynix supports meets that, because the floor is
2.34. Read the requirement only if you link an older Nix yourself: registration
is broken on Nix 2.31 and is not expected to be fixed there.
```

```{eval-rst}
.. automodule:: nanopynix.primops
   :members:

.. automodule:: nanopynix.primops.yaml
   :members:
```
