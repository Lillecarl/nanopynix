# `store.pat` gives the rules that every key here follows: the trailing `$` is
# load-bearing, and a name that ends in `Dict` is the shape of a dictionary
# that a helper returns.
nanopynix_bindings.fetchers.__prefix__$:
    \from typing import TypedDict

    \from nanopynix_bindings.store import Store

    # The functional form, because `from` is a Python keyword and cannot be a
    # field name in the class form. The key keeps Nix's own name: `from` and
    # `to` are what `registry.json` calls the two halves of an entry.
    #
    # `type` is the registry layer, as `Registry::RegistryType` names it in
    # lower case: "flag", "user", "system", "global" or "custom".
    RegistryEntryDict = TypedDict(
        "RegistryEntryDict",
        {
            "type": str,
            "from": str,
            "to": str,
            "exact": bool,
            "extra_attrs": dict[str, str | int | bool],
        },
    )

    # What one write to a registry file did. `path` is the file, `removed`
    # counts the entries the write replaced, `to` is the new target and
    # `locked` says whether that target carries a revision. `to` and `locked`
    # are None for an operation that resolves nothing.
    class RegistryWriteDict(TypedDict):
        path: str
        removed: int
        to: str | None
        locked: bool | None

nanopynix_bindings.fetchers.Input.get_fingerprint$:
    def get_fingerprint(self, store: Store) -> str | None: ...

nanopynix_bindings.fetchers.Input.to_attrs$:
    def to_attrs(self) -> dict[str, str | int | bool]: ...

nanopynix_bindings.fetchers.list_registry_entries$:
    def list_registry_entries(store: Store, fetch_settings: Mapping[str, str] = {}) -> list[RegistryEntryDict]: ...

nanopynix_bindings.fetchers.user_registry_path$:
    def user_registry_path() -> str: ...

nanopynix_bindings.fetchers.registry_add$:
    def registry_add(path: str, from_url: str, to_url: str, fetch_settings: Mapping[str, str] = {}) -> RegistryWriteDict: ...

nanopynix_bindings.fetchers.registry_remove$:
    def registry_remove(path: str, from_url: str, fetch_settings: Mapping[str, str] = {}) -> RegistryWriteDict: ...

nanopynix_bindings.fetchers.registry_pin$:
    def registry_pin(store: Store, path: str, url: str, locked_url: str = "", fetch_settings: Mapping[str, str] = {}) -> RegistryWriteDict: ...
