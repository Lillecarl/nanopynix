nanopynix_fetchers.__prefix__:
    from nanopynix_store import Store

nanopynix_fetchers.Input.__init__:
    def __init__(self, input: "nix::fetchers::Input") -> None: ...
    @overload
    def __init__(self) -> None: ...

nanopynix_fetchers.Input.get_fingerprint:
    def get_fingerprint(self, store: Store) -> str | None: ...
