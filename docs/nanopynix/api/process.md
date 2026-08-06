# Process setup

Two functions that act on the operating-system process rather than on a
session, a store or an evaluator. A host application calls each one once,
before it opens anything.

They are here together because they share that scope, and because neither
belongs to an engine: `nanopynix.rpc` and `nanopynix.inproc` both run after
`init_libstore` has run, and the process title says which program is running,
not which engine it chose.

## Initialise libstore

`init_libstore` is the one Nix initialisation entry point nanopynix offers.
Call it before you open a store. Its docstring gives the reason it also
enables the default experimental features, which is a correctness matter and
not a convenience.

```{eval-rst}
.. autofunction:: nanopynix.init_libstore
```

## Name the process

`set_manager_title` renames the current process, so `ps` and `top` show the
program rather than `python`. Each worker subprocess gets its own short name
from the same mechanism, which is what makes a pool of workers readable in a
process list.

`pynix` calls it at startup with its own name, and that is the pattern to
copy: pass the name of your program, once, before you open a session.

```{eval-rst}
.. autofunction:: nanopynix.set_manager_title
```

## A session across fork()

**A session does not survive a `fork()`, on either engine, and this library
refuses rather than repairs.** Open a session in the process that uses it: fork
first, then open.

The child of an `nanopynix.rpc` session holds the same pipe to the same serial
worker. Two processes then write to one worker, and the protocol desynchronises.
The child also holds the worker's pid, and `Process.terminate()` asks no
question about which process is calling, so a teardown there can kill the
parent's worker.

The child of an `nanopynix.inproc` session holds a thread pool whose threads no
longer exist. `fork()` keeps only the calling thread, so the child submits work
that no thread takes, and waits for a result that never comes. The child also
inherits an initialised libexpr and a Boehm thread table that lists threads that
are gone.

Every entry point in a forked process raises
{class}`~nanopynix.ForkedSessionError`. Teardown is silent instead: `close()`
and `__aexit__` return without touching anything, because the resources belong
to the parent and an exception there would replace whatever sent the child down
that path.

Two mechanisms find the fork, because each one misses what the other catches.
`os.register_at_fork` sees `os.fork()` and every `multiprocessing` start method
that forks. A comparison against `os.getpid()` sees a raw `fork(2)` through
`ctypes`, which the hook does not. A `subprocess` is not a fork, and neither
mechanism reports one.
