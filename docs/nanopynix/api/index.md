# API reference

nanopynix's public API is everything importable from the top-level
`nanopynix` package (`import nanopynix`). Modules with a leading underscore
(`nanopynix._session`, `nanopynix._pool`, ...) are internal implementation
details — their public classes are re-exported at the package root and
documented here under that public name.

`nanopynix.inproc` is the exception — it is a public module in its own
right (not re-exported at the package root), documented separately below.
See {doc}`../architecture` for how it relates to `Session`/`Store`/`EvalSession`.

```{toctree}
:maxdepth: 1

session
store
eval
inproc
settings
models
protocols
exceptions
primops
```
