# Models

Data types crossing the C++/Python boundary. Most are re-exported directly
from the generated proto messages; a few (`StorePath`, `LogEvent`) add
Python-side convenience methods.

An attribute map from Nix -- `RegistryEntry.extra_attrs`, `Input.attrs`,
`FlakeRef.attrs` -- holds an `AttrsValue`, which is a oneof of the three types
`fetchers::Attrs` can hold. {func}`~nanopynix.attrs_to_python` unwraps a whole
map into plain JSON scalars, which is what a caller that prints or serialises
one wants.

An `error` log event carries Nix's structured detail beside the flat message,
in {attr}`~nanopynix.LogEvent.error_info`. It is the dict
{attr}`~nanopynix.NixError.info` carries, from the same C++ builder, so a
warning and an exception describe a position the same way. Nix fills what it
has: `builtins.warn` sets `is_from_expr` and leaves `pos` empty, while a build
failure carries a trace.

```{eval-rst}
.. autoclass:: nanopynix.StorePath
   :members:

.. autoclass:: nanopynix.GcResult
   :members:

.. autoclass:: nanopynix.LogEvent
   :members:

.. autoclass:: nanopynix.PathInfo
   :members:

.. autoclass:: nanopynix.Derivation
   :members:

.. autoclass:: nanopynix.MissingInfo
   :members:

.. autoclass:: nanopynix.NixType
   :members:

.. autoclass:: nanopynix.PrimOpSpec
   :members:

.. autoclass:: nanopynix.DerivedPath
   :members:

.. autoclass:: nanopynix.BuildResult
   :members:

.. autoclass:: nanopynix.ResultType
   :members:
   :undoc-members:

.. autoclass:: nanopynix.GcRoot
   :members:

.. autoclass:: nanopynix.DerivationOutput
   :members:

.. autoclass:: nanopynix.DerivationOutputs
   :members:

.. autoclass:: nanopynix.FlakeRef
   :members:

.. autoclass:: nanopynix.Input
   :members:

.. autoclass:: nanopynix.LockedFlake
   :members:

.. autoclass:: nanopynix.LockedNode
   :members:

.. autofunction:: nanopynix.attrs_to_python
```
