# What this lab wants to change in Nix

`ddrn/README.md` says what `builder-rpc-v0` is, and what it costs a planner.
This file says what we want to change in Nix itself, and where each change
goes. Read `ddrn/README.md` first, and its section "Why this, and not recursive
Nix" for the upstream discussion.

The goals below are not yet a plan. Each one names the file that holds the
code, so that a plan can start from the code and not from a search.

## The checkout

`~/Code/ddnix` is a Jujutsu workspace of the Nix checkout at `~/Code/nix`.
Make one, and put it on the default branch:

```console
$ cd ~/Code/nix
$ jj git fetch
$ jj workspace add --name ddnix -r master@origin ~/Code/ddnix
```

`nix/nix-master.nix` reads that directory, so an edit there reaches every
`nanopynixMaster` build with no other step. That file gives the fallback
behaviour and the `NANOPYNIX_NIX_MASTER_SRC` override. The scope reports the
version `2.36.0pre-ddnix`, so a build says which source it read.

## Goal 1: permit `EnsurePath` inside a `builder-rpc-v0` sandbox

**The evaluator cannot run in the sandbox, and one operation is the reason.**
`builtins.storePath` (`src/libexpr/primops.cc`) and `builtins.appendContext`
(`src/libexpr/primops/context.cc`) each make exactly one call to the daemon,
and that call is `Builder::ensurePath`. Every other store call of those two
primops is path arithmetic that `StoreDirConfig` answers with no daemon.

The restricted store already implements the safe form of the operation
(`src/libstore/restricted-store.cc`):

```cpp
void RestrictedBuilder::ensurePath(const StorePath & path)
{
    if (!goal.isAllowed(path))
        throw InvalidPath("cannot substitute unknown path '%s' in recursive Nix", ...);
    /* Nothing to be done; 'path' must already be valid. */
}
```

That code substitutes nothing. It asserts that the path is in the input closure
of the build, or that this builder added the path. The `validOperations`
allowlist in `src/libstore/daemon.cc` refuses the operation before that code
runs.

**The change is one entry in that array.** The same argument is already in the
array, one entry earlier, for `IsValidPath`: "restricted store will prevent it
from seeing derivations it shouldn't".

**Done.** Two things were open, and the measurement answered both.

- `RestrictedStore::getBuilder` is `unreachable()`, so the daemon has to
  receive the restricted builder from somewhere else. It does:
  `DerivationBuildingGoal::processDaemonConnection`
  (`src/libstore/build/derivation-building-goal.cc`) passes
  `makeRestrictedBuilder(freshWorker, context)` to `daemon::processConnection`
  for both recursive flags.
- `builtins.storePath` skips `ensurePath` when `state.storeFS->getMount(...)`
  finds the path, and the closure is bind-mounted in the sandbox, so the guard
  might have made the primop work already. **It does not fire.** The mount
  table fills lazily and holds no entry for a closure path that nothing
  touched. With the allowlist entry removed again, `builtins.storePath` and
  `builtins.appendContext` both fail with "Operation 10 not allowed inside
  derivation". `tests/functional/dyn-drv/sandbox-eval.sh` covers both primops
  and the refusal of a path outside the closure.

## Goal 2: let a submitted derivation carry its own name

**Today the name of the planner has to be the name of its result.**
`src/libstore/build/derivation-check.cc` compares the name of the submitted
object with `outputPathName(drv.name, "out")`. The submitted object is the root
`.drv` of the graph, and a `.drv` is named `<root name>.drv`, so a planner that
makes `graph` must itself be named `graph.drv`. It declares, in advance, the
name of a result that its own builder computes.

`ddrn/NIX-FACTS.md`, section "Why the `.drv` naming schema exists", gives the
whole chain and the commit that added each link. The short form: the `.drv`
suffix is a type tag that the store enforces, the name rule of an output is
what keeps a fixed content-addressed output on the path its name gives, and
neither rule was written with a planner in mind.

**The change replaces the name check with a check of the derivation.** A
submitted output that is a derivation, and whose declared output floats, gets a
different rule: the object must parse as a derivation, and `computeStorePath`
of that derivation must give the path where the object sits. That is a stronger
identity than a name, and it holds the type-tag invariant directly.

**Done.** `enum struct OutputSource` tells `checkOutputs` whether the outputs
came from a build or from a submission, and `isSubmittedDerivation` holds the
new rule. Four things were open, and each has an answer.

- **The relaxation reaches one output, and not the whole derivation.** A
  planner with two outputs submits a derivation for one and an ordinary store
  object for the other, and the name rule still holds the second one.
  `tests/functional/dyn-drv/submit-drv-any-name.sh` asserts both.
- **A fixed content-addressed output keeps the name rule.**
  `DerivationOutput::CAFixed::path` derives the output path from
  `outputPathName(drvName, outputName)`, so a relaxation there would let
  `queryPartialDerivationOutputMap` name one path and the realisation name
  another. `isSubmittedDerivation` returns false for a fixed output, and
  `submit-drv-broken.sh` asserts the refusal.
- **The ingestion method still has to agree.** Every derivation ingests as
  text, and the `CAFloating` branch of `checkCAOutput` compares the declared
  method with the method of the submitted object. So a planner that submits a
  derivation declares `outputHashMode = "text"`. The name is free; the method
  is not.
- **A store object that is not a derivation cannot reach the check.**
  `LocalStore::registerValidPath` parses every path whose name ends in `.drv`,
  so the store refuses one that does not parse. The catch in
  `isSubmittedDerivation` is a backstop for a store that does not, and it turns
  a parse error into a rejection of the output.

## Goal 3: let the evaluator submit what it wrote

`nix eval --submit <output-name>` registers the derivation that the expression
gives, as that output of the derivation that runs now.

A planner needed two steps before this flag: write the graph, read the store
path of the root, and then run `nix store submit-output` with that path. The
evaluator already writes each derivation of the graph through the restricted
socket, so the second step needs no separate command.

**Done.** Two things were open, and reading the code answered both.

- **A derivation stands for its output, so the flag takes the `drvPath` field
  of the resulting deriving path.** The build that runs now makes the
  derivation, and no build made the output of that derivation yet.
- **`SubmitOutput` accepts an `Opaque` deriving path only**
  (`derivation-builder-impl.hh`, which links NixOS/nix#12727), so a nested
  deriving path gets a clear refusal rather than a confusing one.

`tests/functional/dyn-drv/eval-submit.sh` runs the evaluator inside the
sandbox, over four graphs: one of floating content-addressed derivations, one
of input-addressed derivations, one input-addressed root over a floating child,
and one of two levels where the root of the first level is a second planner. It
also asserts that two evaluators in two sandboxes give the same root derivation
path.

## What the three goals gave

`ddrn/examples/evaluated-graph` is the result, beside
`ddrn/examples/submitted-graph`, which stays as the record of what the released
protocol needs. Three things went away:

- **The ATerm writer.** `plan.py` gives the evaluator a Nix expression.
  Interpolating one derivation into the script of another gives the dependency
  and the output path together, and the ATerm form has to compute both.
- **The name coupling.** The outer derivation is named `planner`, and the root
  that it submits is named `graph`.
- **The limit to floating outputs.** One `EvalState` writes every derivation of
  the graph and memoises each hash modulo as it goes, so an input-addressed
  child needs no read of a `.drv` back out of the store. The graph of the
  example has one child of each kind.

Two capabilities went into nanopynix for this, and both are of general use:
`Store.add_to_store` takes a `references` list, and `Store.print_store_path` is
the inverse of `Store.parse_store_path`.

## The `.drvref` design, and why it went away

An earlier attempt gave the reference its own extension. `$out` held a file
whose content was the store path of the root `.drv`, and the trampoline
followed that path. `~/Code/ddnix` holds the prototype on the bookmark
`ensure-path-and-drvref`.

Two measurements ended it. **The follow has to reach every reader, and not only
the goal that builds.** `nix derivation show` returned an empty result and
`nix build --dry-run --json` threw, because each one resolves the deriving path
and reads the result as a derivation without going through the trampoline.
Four call sites needed the follow, and a fifth would appear with the next
reader.

**The indirection also buys nothing that the relaxation does not.** A planner
wants to name a result that it computes, and the name rule was the only thing
in the way.

## Not goals

- `read-only` splits into two flags. The flag makes `storePath` skip
  `ensurePath`, and it also makes `derivationStrict` compute a derivation path
  rather than write one. A planner needs the first and not the second. This is
  a real defect, and it is not on the path of the three goals above.
- The two upstream defects that `ddrn/README.md` records. `SubmitStore::require`
  has no definition, and `Store::writeDerivation` does not prime
  `nix::derivation::hashes`. Both have a workaround in nanopynix already, and
  the second one is why the graph above takes content-addressed outputs.
