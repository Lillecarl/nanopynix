# TODO

# HandleKind type safety
`"store"`/`"eval"`/`"value"`/`"locked_flake"` handle-kind tags are bare strings
scattered across `_worker.py`/`_worker_eval.py`/`_worker_store.py`/
`_handle_registry.py` (and now `_service_adapter.py`'s `HandleArgSpec.kind`).
Investigate a shared `HandleKind` `Literal`/enum type reused by
`HandleRegistry.get_typed` and friends for real type-checked kind tags instead
of ad hoc strings.
