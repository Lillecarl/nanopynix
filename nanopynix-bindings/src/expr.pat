nanopynix_bindings.expr.__prefix__$:
    type ValueType = int | float | bool | str | None | list[ValueType] | dict[str, ValueType]
    class EditLocation(TypedDict):
        path: str
        line: int
    class Doc(TypedDict):
        name: str | None
        args: list[str]
        arity: int
        doc: str
        path: str | None
        line: int
    class AttrDoc(TypedDict):
        path: str
        line: int
        doc: str | None
    class ReplSelection(TypedDict):
        name: str
        attrs: Value
    \from collections.abc import Callable, Sequence
    \from typing import TypedDict, overload
    \from nanopynix_bindings.store import Store

nanopynix_bindings.expr.register_primop$:
    def register_primop(
        name: str,
        arity: int,
        arg_names: Sequence[str],
        doc: str,
        callback: Callable[..., ValueType],
    ) -> None: ...

# stubgen drops a module function whose name starts with an underscore unless a
# pattern names it, which is why `_enter_evaluator_thread` is absent from the
# stub and its call sites carry a blanket `type: ignore`. These three are named
# so that pyright checks the calls instead.
nanopynix_bindings.expr._check_value_alignment$:
    def _check_value_alignment(address: int) -> None: ...

nanopynix_bindings.expr._gc_collect$:
    def _gc_collect() -> None: ...

nanopynix_bindings.expr._gc_finalizer_self_test$:
    def _gc_finalizer_self_test(count: int, size: int) -> int: ...

nanopynix_bindings.expr._gc_repl_env_finalized$:
    def _gc_repl_env_finalized() -> int: ...

nanopynix_bindings.expr._gc_stats$:
    def _gc_stats() -> dict[str, int]: ...

nanopynix_bindings.expr.Value.edit_location$:
    def edit_location(self) -> EditLocation: ...

nanopynix_bindings.expr.Value.get_doc$:
    def get_doc(self) -> Doc | None: ...

nanopynix_bindings.expr.Value.attr_doc$:
    def attr_doc(self, name: str) -> AttrDoc | None: ...

nanopynix_bindings.expr.Value.to_python$:
    def to_python(self) -> ValueType: ...

nanopynix_bindings.expr.Value._release$:
    def _release(self) -> None: ...

nanopynix_bindings.expr.Value.to_json$:
    def to_json(self, *, copy_to_store: bool = False) -> ValueType: ...

nanopynix_bindings.expr.Value.build$:
    def build(self, build_store: Store | None = None, build_mode: int = 0, eval_store: Store | None = None) -> dict[str, object]: ...

nanopynix_bindings.expr.EvalState.__init__$:
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

nanopynix_bindings.expr.EvalState.set_eval_setting$:
    def set_eval_setting(self, name: str, value: str) -> None: ...
nanopynix_bindings.expr.EvalState.set_fetch_setting$:
    def set_fetch_setting(self, name: str, value: str) -> None: ...
nanopynix_bindings.expr.EvalState.repl_select$:
    def repl_select(self, expr: str, path: str = "<string>") -> ReplSelection | None: ...
