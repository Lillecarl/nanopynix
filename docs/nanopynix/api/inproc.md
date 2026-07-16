# In-process API (L2)

`nanopynix.inproc` mirrors the worker-based `Session`/`Store`/`EvalSession`
API, but runs Nix directly on a dedicated thread in the current process —
no subprocess, no gRPC. See
{doc}`Workers vs in-process <../architecture>` for what that trades off and
when to reach for this module instead of `nanopynix.Session`.

```{eval-rst}
.. automodule:: nanopynix.inproc
   :members:
   :undoc-members:
   :show-inheritance:
```
