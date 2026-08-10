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
  `2.36.0pre20260809_adee4313`. `ddrn/examples/submitted/run.sh` builds it and
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

Three examples do not run this way, because each needs a Nix that the pin of
this repository does not give. Each has a `run.sh` that sets up a private
chroot store and the Nix that it needs:

```console
$ ddrn/examples/submitted/run.sh        # builder-rpc-v0, driven by the nix CLI
$ ddrn/examples/submitted-graph/run.sh  # the same, driven by nanopynix
$ ddrn/examples/evaluated-graph/run.sh  # the evaluator in the sandbox, patched Nix
$ ddrn/examples/venv-graph/run.sh       # a Python environment, as a graph
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
`ddrn/examples/venv-graph` is that same environment once `builder-rpc-v0`
removes the limit, and it does compile such an sdist.

I tried nesting, a planner that emits a planner, as a way around this.
Upstream tests `builtins.outputOf (builtins.outputOf ...)` in
`tests/functional/dyn-drv/eval-outputOf.sh`, so the evaluation side works. My
attempt failed at the store layer: the output of the emitted planner lost its
`.drv` suffix and Nix reported `store path '...-stage2-planner' is not a valid
derivation path`. I did not pursue it, because a chain gives depth and not
breadth, and because the feature below removes the need.

**The cause of that failure is rule 2 above**, and the name is the whole of it.
`outputPathName` gives the output of `stage2-planner` the name
`stage2-planner`, and a planner named `stage2-planner.drv` would have worked.
The name relaxation of this lab removes the rule for a submitted output only,
so it does not reach a planner that writes to `$out`. Nesting does work under
`builder-rpc-v0`: `tests/functional/dyn-drv/eval-submit.sh` follows a graph of
two levels, and `builtins.outputOf` chains three times to reach the file.

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

#### The socket is an allowlist of seven operations

**`builder-rpc-v0` is not `recursive-nix`, and the difference is larger than
the name suggests.** `daemon.cc:326` refuses every operation that this list
does not name:

```cpp
static constexpr std::array validOperations = {
    WorkerProto::Op::AddToStore,
    WorkerProto::Op::AddMultipleToStore,
    WorkerProto::Op::AddToStoreNar,
    WorkerProto::Op::AddToStoreScanning,
    WorkerProto::Op::SubmitOutput,
    WorkerProto::Op::AddTempRoot,
    WorkerProto::Op::IsValidPath,
};
```

A refused operation gives `Operation <n> not allowed inside derivation`.

Three consequences follow, and each one shapes what a planner can do:

- **The builder cannot build.** `BuildPaths` and `BuildPathsWithResults` are
  not on the list. A planner registers a graph; it does not realise one. The
  consumer of the submitted `.drv` realises it, with `builtins.outputOf`.
- **The builder cannot read path information.** `QueryPathInfo` is not on the
  list, so a planner cannot ask the store for the size, the references or the
  NAR hash of a path.
- **The evaluator cannot attach store context.** `builtins.storePath` calls
  `ensurePath` (`primops.cc:2017`), and so does `builtins.appendContext`
  (`context.cc:276`). `EnsurePath` is operation 10, and it is not on the list.
  Both primops skip the call under `read-only`, but `read-only` also makes
  `derivationStrict` *compute* a derivation path rather than write one
  (`primops.cc:1901`), which is the opposite of what a planner needs.

The third consequence is the load-bearing one. A derivation gets its
`inputSrcs` from the string context of its attributes, and the two primops
that attach that context are the two the allowlist forbids. **So a planner
inside this sandbox cannot use the evaluator to write its `.drv` files, and
must write the ATerm itself.** That is what `ddrn/_derivation.py` is for.

An evaluator over a second, unrestricted store (`dummy://`) would side-step
the primops, and the ATerm bytes would still have to reach the real store
through `AddToStore`. This is untested here.

#### The graph that this allowlist permits

`ddrn/examples/submitted-graph` builds one, and each step uses an operation
that the allowlist names. Run it with
`ddrn/examples/submitted-graph/run.sh`.

1. **Build each derivation as a value.** `Derivation.from_dict` takes the same
   shape that `Store.read_derivation` returns, and `Derivation.to_aterm`
   renders it with Nix's own writer. Both need a `StoreDirConfig` and nothing
   else: no store, no daemon and no socket, so neither can be refused.
2. **Let Nix fill in the output paths.** An input-addressed output path comes
   from the hash of the derivation, so a planner cannot know it in advance.
   `Derivation.fill_in_output_paths` computes it, and also sets the output's
   environment variable.
3. **Write each derivation.** `Store.write_derivation` uses `AddTempRoot`,
   `IsValidPath` and `AddToStoreFromDump`, which are all permitted.
4. **Submit the root `.drv`**, and not an output of it. The builder cannot
   build, so the `.drv` is the deliverable. Its consumer realises the graph
   with `builtins.outputOf`.

Two rules of Nix meet at step 4, and one mode satisfies both.

Nix checks the name of the submitted store object against
`outputPathName(drv.name, "out")`, which is `drv.name`
(`derivation-check.cc:109`). The submitted object is a `.drv`, so **the outer
derivation must be named `<something>.drv`.** A derivation may carry such a
name only when it ingests as text and has exactly one output named `out`
(`primops.cc:1815`), and `builder-rpc-v0` needs a content-addressing
derivation (`derivation-builder.cc:482`). Text ingestion is content-addressing,
so `outputHashMode = "text"` meets both rules.

That mode is the one an ordinary dynamic derivation uses, which is the honest
description of the result: **a `builder-rpc-v0` planner is a dynamic
derivation whose `.drv` nanopynix wrote through the socket, rather than one
its builder wrote to `$out`.** What the feature buys is the rest of the graph.
A `$out` of a text-ingested derivation is one file, so it holds one `.drv`;
`AddToStore` puts as many as the planner likes into the store beside it.

##### Why the memo in `write_derivation` matters

Step 2 on the root reaches `pathInputModulo` (`aterm.cc:745`), which looks up
each input derivation in a process-global memo and, on a miss, reads that
`.drv` back out of the store with `readInvalidDerivation`. **That read is not
on the allowlist.** `Store.write_derivation` therefore records the hash modulo
of what it writes, while the value is still in hand. A planner that builds its
graph from the leaves upwards never misses, and so never reads. Nix's own
`writeDerivation` could do this and does not.

#### What three changes to Nix remove

Everything above describes the protocol as it was released. This repository
also patches Nix, and `ddrn/UPSTREAM.md` gives each change and the file that
holds it. `ddrn/examples/evaluated-graph` is the same graph under those
changes, and it is worth reading beside `ddrn/examples/submitted-graph` because
the difference is the argument.

- **The allowlist permits `EnsurePath`.** That is one entry in the array in
  `daemon.cc`, and the restricted builder already answers the operation by
  asserting closure membership and substituting nothing. With that entry, the
  evaluator runs in the sandbox: `plan.py` writes a Nix expression, and the
  ATerm writer of the third consequence above becomes an implementation detail
  of nanopynix rather than a thing each planner needs.
- **A submitted derivation carries its own name.** Nix checks the derivation
  rather than the name: the object must parse, and its own contents must give
  the path where it sits. The name coupling of step 4 goes away, so the outer
  derivation is named `planner` and the root that it submits is named `graph`.
  Text ingestion stays, because every derivation ingests as text.
- **`nix eval --submit` registers what the evaluator wrote.** One command
  evaluates the graph, writes each derivation of it through the socket, and
  submits the root. `ddrn/examples/evaluated-graph/plan.py` reaches the same
  result through nanopynix, with `EvalState.eval_string` and
  `Value.derived_path`.

The memo above stops mattering too, for a third reason. One `EvalState` writes
every derivation of the graph and records each hash modulo as it goes, so a
miss cannot happen inside one planner. The graph of `evaluated-graph` therefore
holds one floating child and one input-addressed child, and the released form
could carry neither without the memo of `write_derivation`.

**None of the three changes is upstream.** Each is a candidate for
[NixOS/nix#15810](https://github.com/NixOS/nix/issues/15810), which asks for a
simpler successor to this protocol and is open.

#### Why this, and not recursive Nix

The PR states the relationship to `recursive-nix` directly:

> recursive-nix has not been moving towards stabilization. This new API
> greatly limits the allowed nix daemon API calls within derivations […] in
> order to reduce attack surface and opportunities for non-reproducability of
> derivations between nix versions.

Four issues give the rest of the reasoning, and they read in this order:

- **[#8602](https://github.com/NixOS/nix/issues/8602)** (2023, open) asks for
  "a very restricted recursive nix socket in the sandbox". It gives the want:
  "for RFC 92 dynamic derivations we want to add derivations to the store from
  within the sandbox. While writing a derivation text to a predefined location
  such as `$out` would get the job done for a single derivation, the real power
  comes from adding multiple derivations."
- **[#13768](https://github.com/NixOS/nix/pull/13768)** (draft) built that
  socket as a separate varlink daemon. #15793 says why it moved: the varlink
  form "required a separate client and server, not reusing any of the existing
  nix daemon code and protocol", so the work went into the standard daemon "to
  reduce the total implementation burden".
- **[#15791](https://github.com/NixOS/nix/issues/15791)** (closed) named the
  three design rules that became the allowlist: register an output
  imperatively rather than leave data in a place; do not let the builder extend
  the sandbox; "disable imperatively building things; perhaps whitelist
  operations extremely stringently in general".
- **[#15810](https://github.com/NixOS/nix/issues/15810)** (open) asks for a
  "simple builder-rpc alternative requiring no client code in derivation". It
  names the cost of the current design: "the stdenv bootstrap and other
  foundational derivations cannot reasonably depend on Nix itself". It sketches
  three successors: a textual pipe, a file system interface under `$NIX_OUT`
  where the client chooses each identifier and computes no hash, and a shim
  that runs after the builder inside the same sandbox.

**The reviewers of #15793 raised the same objection.** edolstra asked "wouldn't
it be nicer to provide a simple textual pipe mechanism […] since the latter
requires a derivation to include Nix (or something that implements enough of
the daemon protocol) in its input closure". roberth answered that this "is very
much a version zero 'worse is better' solution […] for reducing unknown
unknowns for the Nix team", and added: "**it may not move to stabilization
though**". The author agrees: "it is called `-v0` and locked behind an
experimental feature for a reason".

Two conclusions matter to this repository:

- **The protocol does not ask a program to invent the `.drv` format.** RFC 92
  already had a builder write one `.drv`, to `$out`. `builder-rpc-v0` adds the
  rest of the graph, which is the part #8602 calls the real power. The client
  code that writes the ATerm is the cost of that, and `ddrn` pays it once.
- **A successor is likely, and it moves the ATerm out of the client.** The
  file system interface of #15810 would replace `Derivation.to_aterm` and
  `Store.write_derivation` with a directory of files. The planner above it, in
  `ddrn/_plan.py`, does not change. Keep that boundary.

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
==> nix (Nix) 2.36.0pre20260809_adee431
submitted-hello> NIX_REMOTE=unix:///build/.nix-socket
/nix/store/561lqncd629kabjdhpxjqqwcmfmkxz5l-submitted-hello
```

Three things make that work, and each one was needed:

- **A Nix from master.** `nix build github:NixOS/nix/<rev>#nix` gets one, and
  the revision only has to be later than 2026-07-21. Hydra builds master, so an
  older revision substitutes from `cache.nixos.org` and does not compile. Hydra
  lags the branch by some hours, so the revision that `nix/nix-master.nix` pins
  compiles instead, which takes about 20 minutes. Set `NIX_REV` to trade one
  for the other.
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
`ddrn/tests/test_aterm_matches_nix.py` is the answer, and it uses two oracles.
It writes a derivation with `ddrn`, adds the text to the store, and parses it
with **Nix's own parser** through `nanopynix`'s `read_derivation`. It then
gives that parse to `Derivation.from_dict` and writes it again with **Nix's
own writer**, and the two texts must be equal byte for byte. The parser alone
would accept a difference that it tolerates, such as a field in the wrong
order. A disagreement about ATerm is a test failure.

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
`AsyncStore` are the operations that registering a graph needs. Two things
were missing, and both exist now:

- **A `submit_output` store operation.** It is one worker-protocol call,
  `SubmitOutput = 1000`. `Store.submit_output` makes it, and raises
  `Unsupported` on a store that is not a restricted build socket.
- **A store opened on the socket that the sandbox provides.** `NIX_REMOTE` is
  already a store URI, so `open_store(os.environ["NIX_REMOTE"])` is the whole
  of it.

So the ATerm writer of `ddrn` is unnecessary in this mode. Nix writes the
derivation, Nix computes the paths, and Python decides only what the graph is.
`ddrn/examples/submitted-graph/plan.py` is that planner, and it imports no
part of `ddrn`.

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

**`nanopynix` binds all of this now.** `nanopynix_bindings.store` gives a
`StoreDirConfig` class, which takes a store directory and nothing else, and a
`Derivation` class with `from_dict`, `to_dict`, `to_aterm`, `store_path` and
`fill_in_output_paths`. Each of the five takes a `StoreDirConfig`, or a
`Store` for the one that needs the hash of an input, so a host-side tool
computes a path and renders a derivation with no store and no daemon.

That gives the host side of a planner Nix's own arithmetic and Nix's own
serialiser, which is the right authority for a tool that generates plans,
checks them, or explains them. It leaves the in-sandbox copy in `ddrn` as the
one place a private implementation is justified, and it gives the differential
test a second oracle: `test_nix_writes_back_the_same_bytes` in
`ddrn/tests/test_aterm_matches_nix.py` lets Nix parse what `ddrn` wrote, lets
Nix write it again, and compares the two texts byte for byte.

`compute_store_path` remains the nearest thing for a file that exists: it
takes a `Store` and hashes a real file, so it cannot answer "where does a
wheel with this hash land" from a lock file alone.

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

Each of the seven steps below is done.

1. **Done.** `nix/nix-master.nix` builds a Nix from the default branch, and
   `nanopynixMaster` in `default.nix` is the nanopynix scope over it. It is
   off every CI matrix, in the same way and for the same reason as
   `nanopynixZig`.
2. **Done.** `Store.submit_output` is bound, gated on the 2.36 band and
   advertised as `build_info()['capabilities']['store_submit_output']`. It is
   bound on every version and refuses on a Nix that has no such operation, so
   the surface of the module does not vary by Nix version.
3. **Done.** `nanopynix-bindings` compiles against the default branch of Nix.
   That branch changed several APIs that the bindings use, and none of the
   changes has to do with `builder-rpc-v0`. Each one now sits behind
   `NANOPYNIX_NIX_2_36`:

   - `fetchers::Input::fromURL` and `fromAttrs` take different arguments.
   - `Store::ensurePath` and `Store::registerDrvOutput` are gone from `Store`.
     Building goes through `Store::getBuilder()`, and `PyStoreImpl` returns a
     builder that offers the Python hook first.
   - `nix::parseDerivation` is `nix::derivation::parse`.
   - `Logger::Fields` is `std::span<const Field>`, and the `Logger` methods are
     `noexcept`, so every override in `PyLogger` catches what the Python
     callback raises and writes it to stderr.
   - `Store` has pure virtuals that `PyStoreImpl` did not implement.
4. **Done.** `ddrn/examples/submitted-graph` registers a graph of three
   derivations from inside a `builder-rpc-v0` build, and submits the root
   `.drv`. It uses no `nix` binary and no evaluator, because the socket
   permits seven operations and the evaluator needs an eighth.
5. **Done.** `StoreDirConfig` and `Derivation` are bound, and
   `ddrn/tests/test_aterm_matches_nix.py` compares the writer of `ddrn` with
   the writer of Nix byte for byte.
6. **Done.** `ddrn/examples/evaluated-graph` runs the evaluator inside the
   sandbox, through `EvalState.eval_string` and `Value.derived_path`, and
   submits the root that the evaluator wrote. The outer derivation is named
   `planner`, and the root is named `graph`. It needs the three changes to Nix
   that `ddrn/UPSTREAM.md` gives, and it writes no ATerm.

7. **Done.** `ddrn/examples/venv-graph` is `ddrn/examples/venv` as a graph, and
   it is the case that motivated all of it. One node installs each wheel with
   `pypa/installer`, one node builds each source distribution with its PEP 517
   backend, and one node composes a real virtual environment with
   `venv.EnvBuilder`.

   **The builders follow `pyproject.nix`**, which installs with
   `pypa/installer` and merges the members with a script that rewrites the
   shebang of each console script. Both matter: `importlib.metadata` finds a
   package only when an installer wrote its `.dist-info`, and a console script
   runs the wrong interpreter until the shebang points at the `bin/python` of
   the environment.

   **The installer is itself a node of the graph.** The planner resolves it
   from the same lock file, and its node unpacks the wheel rather than
   installing it, because nothing can install the installer. That is the
   bootstrap `pip` performs, expressed as one node.

   **`idna` is in the lock file as a source distribution only.** Its
   `pyproject.toml` asks for `flit_core`, the planner resolves that name
   against the same lock file, and the wheel of `flit_core` becomes another
   node of the same graph. Measured:

   ```text
   node: idna-3.18
     inputDrvs:
       fr6sp6rp…-idna-3.18.tar.gz.drv
       hlr53jdq…-flit-core-4.0.2.drv
     backendPath: /1pf0x0b8…/lib/python3.14/site-packages
     backend: flit_core.buildapi
   ```

   `backendPath` is a downstream placeholder, so the output path of the
   backend is not known until the backend is built. Neither node of that pair
   can exist before the plan runs, and a planner that emits one derivation can
   express neither. That is the case `uv2nix` handles and a plain dynamic
   derivation cannot.

   **A binary wheel gets the loader of this system.** The `ninja` wheel ships
   an ELF executable that names `/lib64/ld-linux-x86-64.so.2` and needs
   `libstdc++.so.6`, and this system has neither. `pyproject.nix` corrects that
   with `autoPatchelfHook`; a graph node runs no setup hook, so the node calls
   `pkgs.auto-patchelf` itself. **The planner decides which node needs it**,
   from the tag of the wheel that `packaging` already parsed, so a pure Python
   node takes no dependency on a compiler library.

   **A local project installs as a PEP 660 editable.** The node builds the
   editable wheel from a copy of the project in the store, and then rewrites
   the path that the wheel recorded, the way `pyproject.nix` does in
   `build/hooks/editable_hook`. The rewrite wraps the replacement in
   `os.path.expandvars`, so the environment reads whichever tree
   `$DDRN_EDITABLE_ROOT` names when the interpreter starts:

   ```text
   import sys; import os.path; sys.path.append(os.path.expandvars('$DDRN_EDITABLE_ROOT/src'))
   ```

   The environment runs, and it is a virtual environment and not a directory
   with a wrapper:

   ```text
   interpreter  /nix/store/rwl64yg3…-demo-venv/bin/python
   prefix       /nix/store/rwl64yg3…-demo-venv
   idna         3.18 xn--eckwd4c7c.xn--zckzah
   ninja        1.13.0
   editable     /nix/store/90hmi2q7…-myapp
     hello from the tree that the check derivation named
   distributions
     certifi==2024.8.30
     charset-normalizer==3.4.4
     idna==3.18
     myapp==0.1.0
     ninja==1.13.0
   entry points ['idna=idna.cli:main']
   ninja
     1.13.0.git.kitware.jobserver-pipe-1
   console script
     idna xn--eckwd4c7c.xn--zckzah
     myapp hello from the tree that the check derivation named
   ```

   `bin/idna` is a console script that the installer wrote, for a package that
   the graph built from source, with a backend the planner resolved from the
   lock file. `bin/ninja` is a binary that came from an index, and it runs
   because the node that installed it patched it.

   `run.sh` then builds the check a second time, against a second tree, and
   both checks read `rwl64yg3…-demo-venv`. **One environment, two sources, no
   rebuild.** That is what the reference to a variable buys, and it is why
   `pyproject.nix` writes one.

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
