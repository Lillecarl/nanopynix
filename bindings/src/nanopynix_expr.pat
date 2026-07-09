nanopynix_expr.__prefix__:
    from __future__ import annotations
    type ValueType = int | float | bool | str | None | list[ValueType] | dict[str, ValueType]
    from nanopynix_store import Store

nanopynix_expr.Value.to_python:
    def to_python(self) -> ValueType: ...

nanopynix_expr.EvalState.__init__:
    @overload
    def __init__(self, store: Store, search_path: Sequence[str] = []) -> None: ...
    @overload
    def __init__(self, store: Store, search_path: Sequence[str] = []) -> None: ...
