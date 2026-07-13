nanopynix_expr.__prefix__:
    type ValueType = int | float | bool | str | None | list[ValueType] | dict[str, ValueType]
    \from collections.abc import Callable, Sequence
    \from nanopynix_store import Store

nanopynix_expr._set_pure_eval:
    def _set_pure_eval(pure: bool) -> None: ...
nanopynix_expr._set_restrict_eval:
    def _set_restrict_eval(restrict: bool) -> None: ...
nanopynix_expr._set_allowed_uris:
    def _set_allowed_uris(uris: list[str]) -> None: ...

nanopynix_expr.register_primop:
    def register_primop(
        name: str,
        arity: int,
        arg_names: Sequence[str],
        doc: str,
        callback: Callable[..., ValueType],
    ) -> None: ...

nanopynix_expr.Value.to_python:
    def to_python(self) -> ValueType: ...

nanopynix_expr.Value.to_json:
    def to_json(self, *, copy_to_store: bool = False) -> ValueType: ...

nanopynix_expr.Value.build:
    def build(self, build_store: Store | None = None, build_mode: int = 0, eval_store: Store | None = None) -> dict[str, object]: ...

nanopynix_expr.EvalState.__init__:
    def __init__(self, store: Store, search_path: Sequence[str] = []) -> None: ...

nanopynix_expr.EvalState.export_value:
    def export_value(self, value: Value) -> int: ...

nanopynix_expr.EvalState.get_exported:
    def get_exported(self, handle: int) -> Value: ...
