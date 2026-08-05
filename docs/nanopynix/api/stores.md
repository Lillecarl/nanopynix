# Stores

Typed models for every store type the linked Nix can open. A store is
configured by its URI: the scheme picks the implementation, and the query
parameters set that implementation's settings.

<!-- example: stores_example.py#model -->
```python
async with (
    nanopynix.rpc.Session() as nix,
    nix.store(stores.Local(root=root, require_sigs=False)) as store,
):
    print(await store.uri())
```

That is the same store as
`nix.store(f"local://?require-sigs=false&root={root}")`, and
{meth}`~nanopynix.stores.StoreConfig.uri` renders exactly that string. A URI
string still works everywhere a model does.

## Why a store setting is not a global setting

Nix registers only three of these names in `globalConfig`: `require-sigs`,
`build-dir` and `system-features`. The other store settings exist per store and
nowhere else, so a URI parameter is the only way to set them. There is no
global for `priority`, `trusted`, `want-mass-query`, `path-info-cache-size`,
`root`, `state`, `log`, `real`, `read-only`, `use-roots-daemon` or
`ignore-gc-delete-failure`.

A store also reads its settings **once**, while Nix constructs it. Changing a
global afterwards does not reach a store that is already open. Measured: with
the global `require-sigs` still true, copying an unsigned path failed into a
store opened without the parameter and succeeded into one opened with
`require-sigs=false` in its URI.

This is why the session's store defaults are rendered into the URI of each
store rather than applied as globals. {func}`~nanopynix.stores.resolve_store_spec`
is what merges them, and a value set on the store itself always wins.

## The default store gets more than one connection

Nix gives a remote store one connection, and a store operation holds it for as
long as the operation runs. A build holds it until the builder exits, so two
concurrent builds through the daemon run one after the other. `nix build` never
meets this, because one command holds one connection and gets its parallelism
from `max-jobs` inside a single build. A library whose callers run store
operations at the same time is the case that default does not fit.

So opening a daemon store asks for {data}`~nanopynix.stores.DAEMON_MAX_CONNECTIONS`,
which is eight. Measured on four concurrent builds of a two-second derivation:
the four requests started 8.1 seconds apart with one connection, and 0.4
seconds apart with eight.

Eight, and not more, because each connection is a separate `nix-daemon` child
with its own build slots. Concurrent local builds are therefore connections
times `max-jobs`, and NixOS sets `max-jobs` to `auto`. Eight keeps that product
near the size of the `build-users-group` pool.

**The number is not a field default, and that is deliberate.** A model
describes a URI, and `None` is how it says "the URI did not state this". A
field default that is not `None` makes that unsayable:
`parse("unix://")` would answer a store carrying a limit the URI never carried.
So the number goes on where a store is about to be opened —
{func}`~nanopynix.stores.resolve_store_spec` for a
{class}`~nanopynix.stores.Daemon` a caller named, and
{func}`~nanopynix.stores.resolve_auto_uri` for an `auto` that became one.
`stores.Daemon().uri()` still renders `unix://`.

A pool costs nothing until it is used. `nix::Pool` opens a connection only when
every existing one is busy, so this raises a ceiling rather than opening eight
sockets. Pass `max_connections` to state your own limit, or a URI string to get
exactly the store you wrote.

## Two settings named `store`

Nix uses the name `store` for two unrelated settings. On a store it is the
*logical location of the store directory*; in `globalConfig` it is the *URL of
the store to use*. Two stores must agree on the first before a path can be
copied between them.

The global one keeps the plain name on
{class}`~nanopynix.NixGlobalSettings`. The per-store one is `store_dir` here,
and renders back to `store` in the URI. Pydantic accepts a duplicate alias in
silence and sets both fields from one key, so keeping them apart is not
cosmetic.

## The hierarchy

The classes mirror Nix's own C++ configuration classes:

- {class}`~nanopynix.stores.StoreConfig` — the six settings every type accepts
- {class}`~nanopynix.stores.Auto` — whatever the host is configured to use
- {class}`~nanopynix.stores.Dummy` — a store that holds nothing
- {class}`~nanopynix.stores.Local`, {class}`~nanopynix.stores.LocalOverlay`
- {class}`~nanopynix.stores.Daemon` — over a Unix socket
- {class}`~nanopynix.stores.Ssh`, {class}`~nanopynix.stores.SshNg`,
  {class}`~nanopynix.stores.MountedSshNg`
- {class}`~nanopynix.stores.FileBinaryCache`,
  {class}`~nanopynix.stores.HttpBinaryCache`,
  {class}`~nanopynix.stores.S3BinaryCache`

{func}`~nanopynix.stores.check_all_store_model_drift` compares each model
against Nix's live store registry, which is built by one static initialiser per
linked store implementation. It therefore reports what *this* build can open
rather than what Nix documents.

```{eval-rst}
.. automodule:: nanopynix.stores
   :members:
   :undoc-members:
   :member-order: bysource

.. autoclass:: nanopynix.StoreImpl
   :members:

.. autodata:: nanopynix.store_impl.DISPATCHABLE_METHODS
```
