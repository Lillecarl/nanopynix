nanopynix_expr.__prefix__:
    type ValueType = int | float | bool | str | None | list[ValueType] | dict[str, ValueType]
    \from nanopynix_store import Store

nanopynix_expr.Value.to_python:
    def to_python(self) -> ValueType: ...

nanopynix_expr.EvalState.__init__:
    def __init__(self, store: Store, search_path: Sequence[str] = []) -> None: ...

nanopynix_expr.EvalState.export_value:
    def export_value(self, value: Value) -> int: ...

nanopynix_expr.EvalState.get_exported:
    def get_exported(self, handle: int) -> Value: ...
