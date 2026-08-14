nanopynix_bindings.fetchers.__prefix__$:
    \from nanopynix_bindings.store import Store

nanopynix_bindings.fetchers.Input.get_fingerprint$:
    def get_fingerprint(self, store: Store) -> str | None: ...

nanopynix_bindings.fetchers.Input.to_attrs$:
    def to_attrs(self) -> dict[str, str | int | bool]: ...
