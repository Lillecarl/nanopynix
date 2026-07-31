"""Declares what `_ext` offers the modules beside it: seven submodules.

Hand-written and source-tree-only, unlike the generated per-area stubs. A type
checker reading this checkout finds no compiled `_ext` -- the extension only
exists in a built tree -- so without this the seven republishing modules would
each fail to resolve their one import. Consumers never see it: they resolve
`nanopynix_bindings` from an installed copy, where the real extension and the
generated `errors.pyi`/`expr.pyi`/... are what answer.

`ModuleType` rather than anything richer on purpose. The contents of each area
are described by its own generated stub; restating any of it here would be a
second copy to keep in sync, and nothing reads `_ext` except the seven modules
that immediately hand their submodule over to `sys.modules`.
"""

from types import ModuleType

errors: ModuleType
signals: ModuleType
util: ModuleType
store: ModuleType
expr: ModuleType
fetchers: ModuleType
flake: ModuleType
