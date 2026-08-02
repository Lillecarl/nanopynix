# Namespaced workers and the overlay store

On Linux a worker can run in its own user namespace, with an OverlayFS mounted
at `/nix/store`: the host store as the lower layer, a scratch directory as the
upper layer. Three things follow from that.

- A build sees every path the host store already has, and nothing is copied in.
- The host store cannot change, because the mount is in a private mount
  namespace. Every other process on the machine keeps the store it had.
- The worker owns the Nix settings of that store, including `sandbox` and
  `sandbox-paths`. Against the host store the daemon owns them, and a client
  cannot relax them.

The last point is the reason this exists. To build with an impure path in the
sandbox, you previously had to copy the whole closure into a temporary store.
A local overlay store gives you the host's paths *and* your own sandbox.

Results do not reach the host store on their own. Copy them when you want them,
with an ordinary `copy_closure` to a `daemon` store.

## Using it

```python
import tempfile
import nanopynix

support = nanopynix.probe_namespace_support()
if not support:
    raise SystemExit(f"not available here: {support.reason}")

with tempfile.TemporaryDirectory() as root:
    spec = nanopynix.OverlayNamespace.under(root)
    async with (
        nanopynix.rpc.Session(namespace=spec) as session,
        session.store() as overlay,
        session.store("daemon") as host,
    ):
        # ... build in `overlay` ...
        await overlay.copy_closure([path], host, check_sigs=False)
```

`Session` makes the overlay its default store, so `session.store()` opens it.
It also turns on the `local-overlay-store` experimental feature and sets the
two settings the store cannot work without. Name a store explicitly, as
`session.store("daemon")` does above, to reach past the overlay.

Use `daemon` by name for the host store, never `auto`. The worker is root
inside its own user namespace, so `auto` resolves to a *local* store at
`/nix/store` -- which is the overlay mount, not the host store.

## From the command line

```console
$ pynix build -f ./default.nix --namespaced
$ pynix build -f ./default.nix --namespaced --sandbox-path /ccache=~/.ccache
$ pynix build -f ./default.nix --overlay-dir ~/.cache/pynix/overlay
```

`--namespaced` builds in a throwaway overlay and copies the outputs into the
host store when the build succeeds. `--no-copy-back` keeps them in the
namespace, where they disappear with the worker. `--overlay-dir` keeps the
upper layer, so a later build reuses what an earlier one produced.

## Requirements

- Linux, with unprivileged user namespaces enabled.
- A filesystem for the layers that supports user extended attributes. An
  unprivileged mount cannot write the `trusted.overlay.*` attributes OverlayFS
  uses by default, so the mount asks for `userxattr` instead.
- A trusted Nix user, for the copy back. The daemon refuses unsigned paths from
  an untrusted one.

`probe_namespace_support()` answers all of the first two at once. It runs the
real sequence in a throwaway process rather than reading capability flags,
because a host can allow user namespaces and still fail to mount. `Session`
calls it before it starts a namespaced worker, so a host that cannot do this
gives you the reason rather than a worker that died.

## Why it happens at worker start

`unshare(CLONE_NEWUSER)` fails with `EINVAL` in a process that has more than
one thread, and the worker has an event loop and a Nix executor thread by the
time it can answer an RPC. So there is no "enter a namespace now" call. A
`fork` keeps only the calling thread, which makes the forkserver child
single-threaded, and the namespace is entered there -- inside
`worker_service_factory`, the last point before the worker starts a thread of
its own.

This makes a namespace a property of the worker, fixed when the session is
created. A running worker cannot move into one.

## API

```{eval-rst}
.. autoclass:: nanopynix.OverlayNamespace
   :members:

.. autoclass:: nanopynix.NamespaceSupport
   :members:

.. autofunction:: nanopynix.probe_namespace_support

.. autofunction:: nanopynix.enter_overlay_namespace
```
