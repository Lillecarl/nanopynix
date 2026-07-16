# API reference

nanopynix's public API is everything importable from the top-level
`nanopynix` package (`import nanopynix`). Modules with a leading underscore
(`nanopynix._session`, `nanopynix._pool`, ...) are internal implementation
details — their public classes are re-exported at the package root and
documented here under that public name.

```{toctree}
:maxdepth: 1

session
store
eval
settings
models
protocols
exceptions
primops
```
