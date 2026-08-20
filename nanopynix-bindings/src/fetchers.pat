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

nanopynix_bindings.fetchers.Input.get_fingerprint$:
    def get_fingerprint(self, store: Store) -> str | None: ...

nanopynix_bindings.fetchers.Input.to_attrs$:
    def to_attrs(self) -> dict[str, str | int | bool]: ...

nanopynix_bindings.fetchers.list_registry_entries$:
    def list_registry_entries(store: Store, fetch_settings: Mapping[str, str] = {}) -> list[RegistryEntryDict]: ...
