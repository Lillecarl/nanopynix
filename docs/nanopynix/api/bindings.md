# The compiled bindings

The engine is huggorm's generated bindings, `huggorm_bindings`, a set of
nanobind extensions that link against Nix's own C++ libraries. Each name below
is one that the `nanopynix` package exports over it, so each one is a public
promise of this project.

**Most callers do not need this page.** {class}`~nanopynix.rpc.Session` and
{mod}`nanopynix.inproc` wrap every name here in an async API that manages the
evaluator, the store and the process lifecycle for you. Read this page when you
extend nanopynix, when you write a tool that runs before a session exists, or
when you want to know what a higher-level call does underneath.

## Three kinds of name

The names are not one surface, and the risk of calling one directly differs
a lot between the three groups.

Process-wide state
: `enable_experimental_feature`, `install_logger`, `remove_logger` and
  `register_primop` change the whole process. Nix keeps this state in global
  C++ objects, so a call here reaches every store and every evaluator,
  including the ones a session opened already. A session routes the same
  settings per scope, which is why the session route is the documented one.
  `set_verbosity` sits in this group by history rather than by scope: it now
  writes one thread's level, and the paragraph below says what that means.

Lifecycle and extension points
: `init_libexpr`, `process_connection` and `register_store_implementation` are
  not day-to-day calls. `init_libexpr` runs once for the process, and a session
  already runs it. The other two exist so that you can build a daemon or a
  store type, and each one has its own contract below.

Plain queries
: `build_info`, `current_system`, `eval_counters_enabled`, `get_verbosity`,
  `is_pseudo_url` and `list_settings` read state. They are safe to call.
  `BuildMode` and `PrimopError` are types that the async API takes and a
  primop raises.

## Thread confinement, and why the async API exists

Nix's evaluator is not thread-safe, and nanopynix exports no raw evaluator. An evaluator
belongs to one thread, and a value belongs to the evaluator that made it.
Every call into the bindings also blocks, because the C++ side has no
`await`.

The two engines exist to solve that. The in-process engine runs the bindings on
a dedicated thread and hands the caller a future. The rpc engine runs them in a
separate worker process. Either way the caller writes `await`, and neither one
lets a value cross to an evaluator that does not own it.

## Process and build information

`build_info` reports the Nix version this extension linked against, and the
compile-time capabilities that the version decides. The capability flags are
how nanopynix supports more than one Nix version from one source tree: a
feature that 2.35 has and 2.34 lacks appears here as a boolean rather than as a
version comparison at each call site.

`current_system` returns the value that `builtins.currentSystem` gives, after
the `system` setting is applied.

```{eval-rst}
.. autofunction:: nanopynix.build_info

.. autofunction:: nanopynix.current_system
```

## Logging

Nix uses the word "stderr" for the events of its `Logger`, and not for the
stderr of the operating system. `install_logger` replaces that logger with a
Python callback, which then receives every log event of the process.

```{warning}
An exception that the callback raises crashes the process. The callback runs
on Nix's own thread, inside C++ code that has no handler for a Python error.
Catch everything inside the callback.
```

`remove_logger` puts Nix's default logger back. `set_verbosity` and
`get_verbosity` control how much the logger receives, on a scale of 0 (error)
to 7 (vomit).

**`set_verbosity` sets the level of the calling thread only.** Nix logs on the
thread that produced the message, and the level lives in a thread-local, so a
call here reaches no other thread. `set_default_verbosity` writes the level
that a thread Nix starts for itself begins at.

Prefer the session route. `Session(log_level=...)` sets the verbosity for that
session's scope, an evaluator may hold a level of its own, and the session
streams the events to an async iterator, so one evaluation's logs stay
separate from another's. The functions here carry none of that: a level set
here is undone by the next dispatched operation, which applies the level of
whatever the caller dispatched through.

```{eval-rst}
.. autofunction:: nanopynix.install_logger

.. autofunction:: nanopynix.remove_logger

.. autofunction:: nanopynix.set_verbosity

.. autofunction:: nanopynix.get_verbosity

.. autofunction:: nanopynix._engine.filter_ansi_escapes
```

`filter_ansi_escapes` is `nix::filterANSIEscapes`, and it keeps the upstream
signature. It reads no configuration and it touches no global state, so it
needs no initialisation and it runs on any thread. Most callers want
`nanopynix.strip_ansi` instead, which is this function with `filter_all` on.

## Global settings

`list_settings` returns the effective value of every global setting that Nix
registered. With `overridden_only=True` it returns only the settings that
something set, which tells a `nix.conf` value apart from a default.

`enable_experimental_feature` turns on one feature, such as `flakes` or
`nix-command`, for the whole process.

```{note}
Nix reads a setting at one of four moments: process start, store construction,
evaluator construction, or the point of use. A call that arrives after the
moment that a setting is read has no effect, and Nix reports no error.
{class}`~nanopynix.NixGlobalSettings` and the per-scope settings of a session
apply each value at the moment that Nix reads it, which is the reason to
prefer them. See {doc}`settings`.
```

```{eval-rst}
.. autofunction:: nanopynix.list_settings

.. autofunction:: nanopynix.enable_experimental_feature
```

## Evaluation

`init_libexpr` initialises Nix's evaluation library for the process. A session
calls it, so a caller who uses a session must not call it again.

An evaluator comes from `session.eval(store)`, which confines it to one thread
and closes it in order. Its values are {class}`~nanopynix.AsyncValue`.

`set_eval_counters_enabled` turns the evaluation counters on, and
`eval_counters_enabled` reports their state. The counters back the numeric
fields of `EvalSession.statistics()`, and Nix leaves them off unless
`NIX_SHOW_STATS` is set, because each increment costs an atomic write.

**Both name the process, and not one evaluator.** `nix::Counter::enabled` is a
static of `libnixexpr`, so an evaluator cannot count while another one beside
it does not, and the `nrExprs` and `nrThunks` fields count every evaluator in
the process. The report is therefore unreliable when one process holds more
than one evaluator, and the inproc engine evaluates in parallel in one
process. Issue #118 tracks the repair, which moves the switch and the counters
onto the evaluator.

`is_pseudo_url` reports whether Nix downloads a string as a tarball, rather
than reading it as a path. It is the first test that `EvalSession.file`
applies to its argument: `channel:nixos-unstable` and a URL
with a scheme that Nix fetches both answer `True`, and `github:NixOS/nixpkgs`,
`<nixpkgs>` and `./default.nix` all answer `False`. Ask this when you classify
such an argument yourself, because the list of schemes belongs to Nix and it
moves with the Nix version. `pynix` uses it to decide which `--file` arguments
it hands over unchanged.

`register_primop(name, arity, callback)` adds a Python function to `builtins`
of every evaluator that opens after the call. Registration is process-wide, and
a second registration of a name replaces the first. `Session(primops=...)` is
the supported route, and it also gives the rpc engine a way to run the
function on the client. See {doc}`primops`.

`PrimopError` is the class that a primop raises to reject its argument.
Nix shows the message of a `PrimopError` **bare**, with no type-name prefix, so
the primop controls exactly what the user reads. A `ValueError` behaves the
same way. Any other class keeps its name as a prefix, which marks the failure
as unexpected rather than deliberate.

```{eval-rst}
.. autofunction:: nanopynix.init_libexpr

.. autofunction:: nanopynix.eval_counters_enabled

.. autofunction:: nanopynix.set_eval_counters_enabled

.. autofunction:: nanopynix.is_pseudo_url

.. autofunction:: nanopynix.register_primop

.. autoexception:: nanopynix.PrimopError
```

## Stores

A store comes from `session.store()`, which takes a URI or one of the typed
models of {doc}`stores`.

`BuildMode` selects what a build does: `Normal` builds what is missing,
`Repair` rebuilds a path whose contents are damaged, and `Check` builds a path
that exists again and compares the result.

`process_connection` serves one connection of the Nix daemon protocol on a file
descriptor that you own. It is how a Python process acts as a Nix daemon. The
`trusted` argument decides what the peer may ask for, and the caller must
decide it, because the binding cannot know how the peer authenticated.

`register_store_implementation` claims a URI scheme for a Python class. The
factory returns a {class}`~nanopynix.StoreImpl` subclass, which lists the
operations that a store may override. Registration is process-wide and
permanent, the same as `register_primop`.

```{eval-rst}
.. autoclass:: nanopynix.BuildMode
   :members:
   :undoc-members:

.. autofunction:: nanopynix.process_connection

.. autofunction:: nanopynix.register_store_implementation
```

## The environment dumper script

Nix embeds `get-env.sh` into the `nix` binary, and no library carries it.
huggorm's bindings ship the file, copied from the Nix source of the version
they link. `get_env_sh_path` returns the path to the installed copy, which is
what `pynix print-dev-env` reads when it rewrites a derivation to dump its
own build environment.

```{eval-rst}
.. autofunction:: nanopynix.get_env_sh_path
```
