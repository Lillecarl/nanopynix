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

Two things to prove, and not to assume:

- `RestrictedStore::getBuilder` is `unreachable()`, so the daemon must receive
  the restricted builder from `derivation-builder.cc`. Confirm that the
  `RecursiveSubmitted` socket gets one.
- `builtins.storePath` skips `ensurePath` when `state.storeFS->getMount(...)`
  finds the path. The closure is bind-mounted in the sandbox. Measure whether
  that guard already fires, because it changes how much Goal 1 buys.

## Goal 2: let `$out` hold a reference to a derivation

**Today the submitted store object is the derivation, and three rules collide
because of it.** `src/libstore/build/derivation-check.cc` compares the name of
the submitted object with `outputPathName(drv.name, "out")`, so the outer
derivation must carry a `.drv` name. `src/libexpr/primops.cc` permits a `.drv`
name only for a derivation that ingests as text and has exactly one output
named `out`. `src/libstore/build/derivation-builder.cc` needs a
content-addressing derivation. Text ingestion satisfies all three at once,
which is a coincidence, and it constrains the shape of every planner.

The alternative is one level of indirection: `$out` holds a store path, and
that path is the root `.drv`. The planner writes the `.drv` through the socket,
as it does now, and then writes the reference.

This is more expressive than the RFC 92 form, in which a builder writes the
ATerm bytes to `$out`. A reference lets the root name siblings that the planner
wrote separately.

## Goal 3: make the trampoline follow the reference

`src/libstore/build/derivation-trampoline-goal.cc` turns "output `out` of
derivation D" into a derivation to build. It calls `resolveDerivedPath`
(`src/libstore/misc.cc`), which returns the output path of D, and it then reads
that path as a derivation with `readDerivation`.

That last step is the assumption to change. The output path must be able to
name the derivation instead of being the derivation.

**A suffix keeps the old form working.** Give the reference file the extension
`.drvref`. The trampoline reads the name of the resolved output path:

- a name that ends in `.drv` is the derivation, which is the behaviour today;
- a name that ends in `.drvref` holds the store path of the derivation, and the
  trampoline reads that file and follows the path.

No existing dynamic derivation changes, because no existing output has the new
extension.

Open questions for the plan:

- Which store validates the reference, and when. A reference to a path that no
  store holds must fail with a clear message, and not with a parse error of a
  derivation.
- Whether `.drvref` needs the same name rule as `.drv` in
  `src/libexpr/primops.cc`. The rule exists to stop an arbitrary derivation
  claiming a `.drv` name.
- Whether the reference holds one path, or a list. One path is enough for a
  root. A list would let a planner submit several roots.
- What `builtins.outputOf` reports when the reference is absent or empty.

## Not goals

- `read-only` splits into two flags. The flag makes `storePath` skip
  `ensurePath`, and it also makes `derivationStrict` compute a derivation path
  rather than write one. A planner needs the first and not the second. This is
  a real defect, and it is not on the path of the three goals above.
- The two upstream defects that `ddrn/README.md` records. `SubmitStore::require`
  has no definition, and `Store::writeDerivation` does not prime
  `nix::derivation::hashes`. Both have a workaround in nanopynix already.
