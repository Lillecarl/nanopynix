# YAML primops

Built-in `builtins.fromYAML`/`builtins.toYAML`-style primops, registered by
passing `yaml_primops()` to `Session(primops=...)`. See
{doc}`../examples` for a runnable walkthrough, including how to register a
custom Python-backed primop alongside these.

## Which reader to call

`fromGoLikeYAML` and `fromGoLikeYAMLStream` read the way **go-yaml v2**
reads. `sigs.k8s.io/yaml` uses that parser, so the Kubernetes API server
decodes with it and Helm renders through it. Call these for a manifest, for
a chart value and for anything else that a cluster reads. Read `v2`
literally: go-yaml v3 is a different dialect, and v3 does not read `0644`
as 420.

That dialect is neither YAML version. It reads `0644` as 420, which is YAML
1.1, and `1e+06` as a number, which is YAML 1.2. No single version reads
both the way Kubernetes does.

`fromYAML` reads YAML 1.2. Call it for a file that is not a manifest.

```{deprecated} 0
`fromYAML11` and `fromYAML11Stream` aim at the same go-yaml v2 and miss.
Issue #307 lists six classes of scalar where they disagree with it, and two
of them break a chart: an unquoted `n` is the string `"n"` here and `false`
to Kubernetes, and an unquoted date stops the whole document. Use
`fromGoLikeYAML`.
```

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
