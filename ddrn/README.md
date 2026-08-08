# ddrn: dynamic derivations, driven from Python

An experiment. The goal is to write a Nix build plan in Python, and to keep
the packaging logic of an ecosystem in that ecosystem's own tools rather than
in a reimplementation in the Nix language.

`uv2nix` and `pyproject.nix` are the motivating case. Both carry a copy of PEP
508 environment markers, PEP 440 version specifiers and PEP 425 compatibility
tags, written in the Nix language. `packaging` is the reference implementation
of all three, and it is Python. A planner runs it unchanged.

Everything below was measured on this machine, with Nix 2.34.8, a local store
and `x86_64-linux`. Each claim names the command that produced it.

## TL;DR

- A derivation whose output is text-hashed **is** a derivation. Python can
  write that output. No `nix` binary and no `recursive-nix` are involved.
  Proved by `ddrn/examples/hello`.
- Nix instantiates a candidate without building it, so a planner can pick a
  subset and Nix builds only that subset. Proved by `ddrn/examples/select`:
  2 of 4 candidates built, and `ddrn/examples/venv`: 3 of 20 wheels
  downloaded.
- A whole Python environment builds this way, with every packaging decision
  made by `packaging` inside the planner and none of it in Nix. Proved by
  `ddrn/examples/venv`.
- **One planner output is exactly one derivation.** A fan-out needs
  `builder-rpc-v0`, which is merged in Nix master and is in no release yet.
- `builder-rpc-v0` **runs on this machine**, on Nix
  `2.36.0pre20260806_9137203`. `ddrn/examples/submitted/run.sh` builds it and
  proves it. That feature is where `nanopynix` belongs. See
  [What `builder-rpc-v0` changes](#what-builder-rpc-v0-changes).

## Running the examples

Each example needs two experimental features:

```console
$ export NIX_CONFIG='experimental-features = nix-command flakes ca-derivations dynamic-derivations'
$ nix build --file ./ddrn/examples hello.result  --no-link --print-out-paths -L
$ nix build --file ./ddrn/examples select.result --no-link --print-out-paths -L
$ nix build --file ./ddrn/examples venv.result   --no-link --print-out-paths -L
```

The tests check the writer of `ddrn` against Nix itself:

```console
$ direnv exec . pytest ddrn/tests
```

## How it works

A **planner** is an ordinary derivation with three extra attributes:

```nix
__contentAddressed = true;
outputHashMode = "text";
outputHashAlgo = "sha256";
```

`text` ingestion tells Nix to read the output back as a derivation.
`builtins.outputOf planner.outPath "out"` then names the output of *that*
derivation. `ddrn/nix/planner.nix` wraps this as `mkPlanner`.

The planner writes one line of ATerm:

```text
Derive([outputs],[inputDrvs],[inputSrcs],system,builder,[args],[(k,v)...])
```

`ddrn.Derivation.to_aterm` produces that line. The emitted derivation is
content-addressed and floating by default, so its output path is the
placeholder constant and the planner performs no hash arithmetic.

### The menu, and why the plan stays lazy

The plan must name derivations that already exist in the store. That is not a
restriction in practice, because **Nix writes every `.drv` file at
instantiation, long before it builds anything**. A Nix expression can
therefore hand the planner a whole menu of unbuilt candidates:

```nix
candidates = map (drv: {
  name = drv.name;
  drv = builtins.unsafeDiscardOutputDependency drv.drvPath;
  outputs = { out = builtins.unsafeDiscardStringContext drv.outPath; };
  meta = { ... };
}) everyCandidate;
```

The two `unsafeDiscard*` builtins are what keep the plan lazy.
`unsafeDiscardOutputDependency` makes the `.drv` file an input source while
the *output* stays unbuilt. `unsafeDiscardStringContext` gives the planner the
output path without a dependency on it. A candidate that the planner rejects
is never built, and never downloaded.

`ddrn/examples/venv` is the whole argument in one file. The Nix half reads a
lock file and makes one `fetchurl` per artefact. It knows nothing about
wheels. The Python half evaluates the markers and ranks the tags, and names
the three wheels it wants:

```text
plan: colorama-0.4.6-py2.py3-none-any.whl: marker excludes this target
plan: 3 of 20 wheels selected
plan:   certifi-2024.8.30-py3-none-any.whl
plan:   charset_normalizer-3.4.4-cp314-cp314-manylinux2014_x86_64...whl
plan:   idna-3.10-py3-none-any.whl
```

Nix downloaded those three. It downloaded neither the 13 other
`charset-normalizer` wheels, nor the pure-Python fallback that the compiled
wheel outranks, nor `colorama`.

## The limit: one output, one derivation

A planner emits exactly one derivation. Two rules of Nix combine to make this
so, and neither has a workaround inside the feature:

1. A text-hashed output is one flat file, so it holds one derivation.
2. An output other than `out` takes the store path name
   `<drv-name>-<output-name>` (`derivations.hh`, `outputPathName`). That name
   does not end in `.drv`, and `StorePath::isDerivation` refuses it. A planner
   therefore cannot emit a second derivation as a second output.

The consequence is the one that matters for a package set: **an emitted
derivation cannot depend on another emitted derivation.** It can depend on
anything the Nix expression instantiated, which is what the menu is for, and
on nothing that the planner invented.

`ddrn/examples/venv` therefore emits a single install derivation over
pre-instantiated fetches. That is enough for a venv. It is not enough for a
build graph in which one generated derivation feeds another, such as compiling
an sdist whose build backend is itself resolved from the lock.

I tried nesting, a planner that emits a planner, as a way around this.
Upstream tests `builtins.outputOf (builtins.outputOf ...)` in
`tests/functional/dyn-drv/eval-outputOf.sh`, so the evaluation side works. My
attempt failed at the store layer: the output of the emitted planner lost its
`.drv` suffix and Nix reported `store path '...-stage2-planner' is not a valid
derivation path`. I did not pursue it, because a chain gives depth and not
breadth, and because the feature below removes the need.

## What `builder-rpc-v0` changes

[NixOS/nix#15793](https://github.com/NixOS/nix/pull/15793), merged on
2026-07-21, adds `builder-rpc-v0`. It gives the builder a **restricted daemon
socket**, and one new worker operation:

```cpp
static constexpr std::string_view featureSubmitOutput = "submit-output";
SubmitOutput = 1000, // Only used within derivations with feature
```

A derivation asks for it with `requiredSystemFeatures = [ "builder-rpc-v0" ]`.
The builder then creates as many store objects as it likes, and registers one
of them as its output with `nix store submit-output <path> out`. `$out` is not
set in such a derivation, which the upstream test
`tests/functional/dyn-drv/non-trivial-submitted.nix` asserts.

This removes the one-derivation limit. A planner can add a `.drv` for every
package, wire them into a real graph, and submit the root.

The PR states the relationship to `recursive-nix` directly:

> recursive-nix has not been moving towards stabilization. This new API
> greatly limits the allowed nix daemon API calls within derivations […] in
> order to reduce attack surface and opportunities for non-reproducability.

**It is in no release.** The newest tag is 2.35.1, and
`src/nix/store-submit-output.cc` exists only on `master`.

This repository already builds against a `git` Nix, and already gives it a
development shell:

```console
$ nix develop --file . nanopynixVersions.git.shell
```

That shell is not new enough. `pkgs.nixVersions.git` in the current pin is
`2.35pre20260619`, and the merge landed on 2026-07-21. The `nixpkgs` input
itself is recent, from 2026-08-03; it is the Nix revision that nixpkgs pins
inside `nixVersions.git` that lags, and a flake update does not move it. To
move it, override the source of `nixComponents_git`, which
`modular/packages.nix` supports through `nixComponents.overrideSource`.

### Running this

`ddrn/examples/submitted/run.sh` runs the feature end to end, and needs no
change to the machine:

```console
$ ddrn/examples/submitted/run.sh
==> 2.36.0pre20260806_9137203
submitted-hello> NIX_REMOTE=unix:///build/.nix-socket
/nix/store/561lqncd629kabjdhpxjqqwcmfmkxz5l-submitted-hello
```

Three things make that work, and each one was needed:

- **A Nix from master, without compiling one.** Hydra builds master, so
  `nix build github:NixOS/nix/<rev>#nix` substitutes from `cache.nixos.org`.
  The revision only has to be later than 2026-07-21.
- **A private chroot store.** `/nix/store` belongs to root here, so an
  ordinary build goes through the system daemon, which is 2.34.8 and has no
  such feature. `nix build --store <dir>` makes the client build in itself,
  which runs the new code. The store *directory* stays `/nix/store`, so every
  dependency still substitutes rather than rebuilds.
- **`--system-features 'builder-rpc-v0'`.** It is a system feature, so the
  store has to advertise it, exactly as `recursive-nix` does.

What the build shows:

- `NIX_REMOTE=unix:///build/.nix-socket`. The socket is an ordinary
  worker-protocol socket, at `tmpDirInSandbox() / ".nix-socket"`
  (`derivation-builder.cc`), serving a `RestrictedStore` with
  `RecursiveFlag::RecursiveSubmitted`.
- `$out` is unset, and the build asserts it. The output arrives only through
  `submit-output`.
- The derivation must be content-addressing. Nix refuses otherwise: *"The
  builder-rpc-v0 feature may only be used with content-addressing
  derivations"*.
- The submitted store object must carry the name the output must have, which
  is the derivation name for `out`. The first attempt failed with:

  ```text
  error: derivation '...-submitted-hello.drv' output 'out'
         (at '/nix/store/...-work') was named 'work',
         expected 'submitted-hello'
  ```

  `nix store add --name submitted-hello ./work` is the fix.

## Where nanopynix fits

Three separate answers, because the context decides which one applies.

### Inside the sandbox, today: pure Python

`ddrn` has no dependency outside the standard library. A planner runs under a
bare `python3`, and every store path in its closure is a path that the build
must fetch. Linking libnixstore into every planner is a cost with no benefit
while the plan is one derivation.

The risk of a private copy of a format is that the copy drifts.
`ddrn/tests/test_aterm_matches_nix.py` is the answer: it writes a derivation
with `ddrn`, adds the text to the store, and parses it with **Nix's own
parser** through `nanopynix`'s `read_derivation`. A disagreement about ATerm
is a test failure.

Nix checks the store path arithmetic too, without being asked. A fixed-output
derivation whose recorded path differs from the one Nix computes is refused:

```text
error: derivation has incorrect environment variable 'out',
       should be '/nix/store/wba5j5zw...-fixed-output'
       but is actually '/nix/store/7mglnj0l...-fixed-output'
```

That message is how the `nar` and `flat` ingestion modes were confirmed
against Nix, and it is why `ddrn.Output.fixed` sets the mode and the path
together.

### Inside the sandbox, with `builder-rpc-v0`: nanopynix

This is the interesting one, and it is what the feature was built for, and it
now runs here.

`NIX_REMOTE` inside the sandbox is an ordinary worker-protocol socket. A
planner is therefore a full store client, and `nanopynix` already speaks that
protocol: `add_to_store`, `read_derivation`, `query_path_info` and the rest of
`AsyncStore` are the operations that registering a graph needs. Two things are
missing:

- **A `submit_output` store operation.** It is one worker-protocol call,
  `SubmitOutput = 1000`, gated on the `submit-output` feature of the
  connection.
- **A store opened on the socket that the sandbox provides.** `NIX_REMOTE` is
  already a store URI, so this may cost nothing beyond a clear error when the
  feature is absent, rather than a failure at the first call.

With those, the whole ATerm writer of `ddrn` becomes unnecessary in this mode:
Nix writes the derivation, Nix computes the paths, and Python decides only
what the graph is.

Everything else `ddrn` does by hand becomes unnecessary: Nix writes the ATerm,
Nix computes the paths, and Python decides what the graph is.

### Outside the sandbox, today: a store-free binding surface

`nix::StoreDirConfig` holds one member, `const std::string & storeDir`, and
its methods are pure:

```cpp
StorePath makeStorePath(std::string_view type, const Hash & hash, std::string_view name) const;
StorePath makeFixedOutputPath(std::string_view name, const FixedOutputInfo & info) const;
```

`Derivation::unparse(const StoreDirConfig &, bool maskOutputs, ...)` and
`parseDerivation(const StoreDirConfig &, std::string &&, std::string_view)`
take the same argument and nothing else. **None of them needs a store, a
daemon or a file system.**

`nanopynix` has no binding for any of this yet. `compute_store_path` is the
nearest thing, and it takes a `Store` and hashes a real file, so it cannot
answer "where does a wheel with this hash land" from a lock file alone.

A `StoreDirConfig` binding would give the host side of a planner Nix's own
arithmetic and Nix's own serialiser, which is the right authority for a tool
that generates plans, checks them, or explains them. It leaves the in-sandbox
copy in `ddrn` as the one place a private implementation is justified, and
gives the differential test a second oracle.

## What is unbuilt on the client side

One gap shows up in every build here:

```text
warning: Ignoring dynamic derivation /nix/store/...-demo-venv.drv.drv^out
         while querying missing paths; not yet implemented
```

`src/libstore/misc.cc:200` is the source. `queryMissing` cannot see through a
dynamic derivation, so `--dry-run`, download-size estimates and build
progress are all blind to whatever the plan turns out to be. The build itself
is correct; only the prediction is missing.

## Traps, each one paid for

- **`argv[0]` is a basename.** Nix runs a builder with the basename of the
  builder as `argv[0]`, and CPython finds `sys.prefix` from `argv[0]`. A
  `python3.withPackages` wrapper therefore cannot find its own packages
  inside a build, and fails with a bare `ModuleNotFoundError`. Put the library
  on `pythonPath` instead. `mkPlanner` documents this.
- **`$BASH` belongs to bash.** An emitted derivation that passes a bash path
  in an environment variable called `BASH` gets `/noshell/bin/bash`, because
  bash sets that variable itself. The generated script then fails with `bad
  interpreter`.
- **Ingestion mode and output path travel together.** See the error quoted
  above. `ddrn.Output.fixed` exists so the two cannot disagree.

## Next steps

Two of the four steps below are done.

1. **Done.** `nix/nix-master.nix` builds a Nix from the default branch, and
   `nanopynixMaster` in `default.nix` is the nanopynix scope over it. It is
   off every CI matrix, in the same way and for the same reason as
   `nanopynixZig`.
2. **Done.** `Store.submit_output` is bound, gated on the 2.36 band and
   advertised as `build_info()['capabilities']['store_submit_output']`. It is
   bound on every version and refuses on a Nix that has no such operation, so
   the surface of the module does not vary by Nix version.
3. **Blocked, and the block is not this feature.** `nanopynix-bindings` does
   not compile against the default branch of Nix. That branch changed several
   APIs that the bindings use, and none of them has to do with
   `builder-rpc-v0`:

   - `fetchers::Input::fromURL` and `fromAttrs` take different arguments.
   - `Store::ensurePath` and `Store::registerDrvOutput` are gone from `Store`.
   - `nix::parseDerivation` is not in that namespace any more.
   - `Logger::Fields` moved, and the `Logger` methods are `noexcept`, so every
     override in `PyLogger` has a looser exception specification.
   - `Store` has pure virtuals that `PyStoreImpl` does not implement, so that
     class is abstract.

   Each one is an ordinary port to a new compatibility band. Together they are
   a piece of work of their own, and they are what stands between here and a
   planner written in Python.
4. Then rewrite `ddrn/examples/venv` as a graph: one derivation per wheel, one
   per install step, and an sdist path that resolves its own build backend.
   That is the case `uv2nix` handles and a plain dynamic derivation cannot.
5. Bind `StoreDirConfig` for the host side, and give
   `ddrn/tests/test_aterm_matches_nix.py` a second oracle.

### Two patches of this repository meet the default branch differently

`nixPatches` in `default.nix` needs its own `"2.36"` entry, because the
fallback is wrong in both directions:

- `emptyBindingsPatch` is **dropped, because upstream fixed the defect.** The
  patch made a shared mutable global `thread_local`. The default branch
  declares `const constinit Bindings Bindings::emptyBindings`, which no thread
  can write.
- `gmtimePatch` is **rebased and kept, because upstream did not.** Both
  `std::gmtime` calls are still there; only the context around one of them
  moved, when `emitTreeAttrs` gained a `callPos` argument.

A patch that stops applying means one of those two things, and they call for
opposite actions. Check which before deleting one.
