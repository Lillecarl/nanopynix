# Core Infrastructure

Base classes, serde contexts, protocol enums, and type aliases that underpin the entire serde system.

---

## Base Classes

### WireModel

The abstract Pydantic base class that auto-generates `to_writer()` and `from_reader()` from type annotations.

```{eval-rst}
.. automodule:: pynixd.serde.wire_message
   :members: WireField, WireModel
```

### WireString

Abstract base for types that are a single length-prefixed UTF-8 string on the wire.

```{eval-rst}
.. automodule:: pynixd.serde.wire_string
   :members:
```

### WireRequest / WireResponse

Operation base classes. `WireRequest` auto-registers each op in `WIRE_REGISTRY`.

```{eval-rst}
.. automodule:: pynixd.serde.wire_ops
   :members:
```

---

## Serde Contexts

Context dataclasses passed through the serialization/deserialization pipeline.

```{eval-rst}
.. automodule:: pynixd.serde.context
   :members:
```

---

## Protocol Enums

```{eval-rst}
.. automodule:: pynixd.serde.protocol
   :members:
```

---

## Auth

```{eval-rst}
.. automodule:: pynixd.serde.auth
   :members:
```

---

## IDs

```{eval-rst}
.. automodule:: pynixd.serde.ids
   :members:
```

---

## Type Aliases

```{eval-rst}
.. automodule:: pynixd.serde.aliases
   :members:
```
