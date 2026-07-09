nanopynix_fetchers.__prefix__:
    \from nanopynix_store import Store

nanopynix_fetchers.Input.__init__:
    def __init__(self, input: object) -> None: ...

nanopynix_fetchers.Input.get_fingerprint:
    def get_fingerprint(self, store: Store) -> str | None: ...

nanopynix_fetchers.Input.to_attrs:
    def to_attrs(self) -> dict[str, str | int | bool]: ...
