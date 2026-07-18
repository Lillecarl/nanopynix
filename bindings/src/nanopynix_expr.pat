nanopynix_expr.__prefix__:
    type ValueType = int | float | bool | str | None | list[ValueType] | dict[str, ValueType]
    \from collections.abc import Callable, Sequence
    \from typing import overload
    \from nanopynix_store import Store

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

nanopynix_expr.Value._release:
    def _release(self) -> None: ...

nanopynix_expr.Value.to_json:
    def to_json(self, *, copy_to_store: bool = False) -> ValueType: ...

nanopynix_expr.Value.build:
    def build(self, build_store: Store | None = None, build_mode: int = 0, eval_store: Store | None = None) -> dict[str, object]: ...

nanopynix_expr.EvalState.__init__:
    @overload
    def __init__(
        self,
        store: Store,
        search_path: Sequence[str] = [],
        eval_settings: dict[str, str] = {},
        fetch_settings: dict[str, str] = {},
    ) -> None: ...
    @overload
    def __init__(
        self,
        store: Store,
        search_path: Sequence[str] = [],
        build_store: Store | None = None,
        eval_settings: dict[str, str] = {},
        fetch_settings: dict[str, str] = {},
    ) -> None: ...
    def __init__(self, *args: object, **kwargs: object) -> None: ...

nanopynix_expr.EvalState.set_eval_setting:
    def set_eval_setting(self, name: str, value: str) -> None: ...
nanopynix_expr.EvalState.set_fetch_setting:
    def set_fetch_setting(self, name: str, value: str) -> None: ...
