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
