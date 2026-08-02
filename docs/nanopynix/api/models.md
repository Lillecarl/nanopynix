# Models

Data types crossing the C++/Python boundary. Most are re-exported directly
from the generated proto messages; a few (`StorePath`, `LogEvent`) add
Python-side convenience methods.

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

.. autoclass:: nanopynix.LockedInput
   :members:
```
