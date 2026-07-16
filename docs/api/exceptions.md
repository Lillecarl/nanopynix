# Exceptions

`NixError` and its subclasses are raised for Nix-originated failures
(classified from the Nix error message, since Nix's C++ exception types
aren't yet bound). `EvalProxyError` and its subclasses are raised for
client-side misuse of a `ValueProxy`/`EvalSession` (e.g. using a value after
its session closed).

```{eval-rst}
.. automodule:: nanopynix.exceptions
   :members:
   :show-inheritance:
```
