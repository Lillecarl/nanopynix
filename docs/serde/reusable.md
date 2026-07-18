# Reusable Wire Types

Shared types used as fields within requests and responses. These are not protocol operations themselves — they are `WireModel` or `WireString` subclasses that appear as field types in the operations above.

---

## WireString Types

Single length-prefixed UTF-8 strings on the wire.

### StorePath

```{eval-rst}
.. automodule:: pynixd.serde.store_path
   :members:
```

### DerivedPath

```{eval-rst}
.. automodule:: pynixd.serde.derived_path
   :members:
```

### DrvOutput

```{eval-rst}
.. automodule:: pynixd.serde.drv_output
   :members:
```

### ContentAddress

```{eval-rst}
.. automodule:: pynixd.serde.content_address
   :members:
```

### NARHash

```{eval-rst}
.. automodule:: pynixd.serde.nar_hash
   :members:
```

### Signature

```{eval-rst}
.. automodule:: pynixd.serde.signature
   :members:
```

---

## WireModel Types

### BasicDerivation

```{eval-rst}
.. automodule:: pynixd.serde.basic_derivation
   :members:
```

### DerivationOutput

```{eval-rst}
.. automodule:: pynixd.serde.derivation_output
   :members:
```

### UnkeyedValidPathInfo

```{eval-rst}
.. automodule:: pynixd.serde.path_info
   :members:
```

### ValidPathInfo

```{eval-rst}
.. automodule:: pynixd.serde.valid_path_info
   :members:
```

### BuildResult

```{eval-rst}
.. automodule:: pynixd.serde.build_result
   :members:
```

### KeyedBuildResult

```{eval-rst}
.. automodule:: pynixd.serde.keyed_build_result
   :members:
```

### OptMicroseconds

```{eval-rst}
.. automodule:: pynixd.serde.opt_microseconds
   :members:
```

### Realisation

```{eval-rst}
.. automodule:: pynixd.serde.realisation
   :members:
```

### Time

```{eval-rst}
.. automodule:: pynixd.serde.wire_time
   :members:
```

---

## Stderr Stream Types

The Nix daemon stderr stream uses a tagged-union wire format: `[uint64 code][message body]...[uint64 STDERR_LAST]`.

```{eval-rst}
.. automodule:: pynixd.serde.logs
   :members:
```
