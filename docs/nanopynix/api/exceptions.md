# Exceptions

`NixError` and its subclasses are raised for Nix-originated failures
(classified from the Nix error message, since Nix's C++ exception types
aren't yet bound). `ObjectMisuseError` and its subclasses are raised when a
nanopynix object is misused without Nix being consulted at all — the wrong
Nix type for the accessor, a value belonging to another evaluator, or an
object whose lifetime has ended.

That last group has its own base, `ObjectLifetimeError`, so
`except ObjectLifetimeError` catches "the thing I am holding is gone"
whichever kind of thing it is: `SessionClosedError`, `StoreClosedError`,
`EvalSessionClosedError`, `ValueReleasedError`, `LockedFlakeReleasedError`.
Both engines raise the same class for the same situation — inproc used to
have its own `Inproc*` names for three of these, which no `except` clause
written against the rpc engine could catch.

```{eval-rst}
.. automodule:: nanopynix.exceptions
   :members:
   :show-inheritance:
```
