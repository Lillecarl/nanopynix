# Common Nix misconceptions

Things that are widely believed about Nix and are not true, written down
because each one has already cost time in this project -- in review comments,
in test design, or in a wrong diagnosis.

Each entry states the misconception, then the truth, then why it matters here.
Everything below is checked against a real Nix, not recalled.

## "Concurrent Nix operations need external locking"

**Truth: the daemon makes all operations safe to run in parallel.** That is
what it is for. Clients do not coordinate with each other, and nothing above
the daemon needs a lock to make concurrent builds, queries, substitutions and
garbage collection safe against one another.

The case that trips people up is garbage collection during a build. It is
safe: the daemon registers a *temporary* GC root for a build's inputs and
outputs (`/nix/var/nix/temproots`) for as long as the build holds them, and the
collector honours those roots. A GC running mid-build cannot delete what that
build is using.

**Why it matters here:** it means parallel test workers can share one daemon
and one store instead of each paying to substitute the same closure into its
own empty store. Do not add a lock, a `filelock`, or an `xdist_group` pin to
"protect" store operations from each other -- there is nothing to protect them
from.

## "Attribute keys with hyphens must be quoted"

**Truth: they must not be, and need not be.** A Nix identifier is
`[a-zA-Z_][a-zA-Z0-9_'-]*`, so `-` is an ordinary identifier character after
the first position:

```console
$ nix eval --impure --expr '{ foo-bar = 1; a-b-c = 2; }'
{ a-b-c = 2; foo-bar = 1; }
```

`buildPhase`, `dont-unpack`, `system-features` are all plain identifiers.
Quoting them is not wrong, just noise -- and Nix drops the quotes when it
prints them back, which is a good way to see what it actually parsed.

## "...but a leading digit is fine"

**Truth: the reverse of the above.** An identifier may not *start* with a
digit, so a key that does has to be quoted -- and this is the case where the
quotes are load-bearing rather than decorative:

```console
$ nix eval --impure --expr '{ 1foo = 1; }'
       at «string»:1:3:
            1| { 1foo = 1; }
             |   ^
$ nix eval --impure --expr '{ 123 = 1; }'
       at «string»:1:3:
            1| { 123 = 1; }
             |   ^
$ nix eval --impure --expr '{ "1foo" = 1; }'
{ "1foo" = 1; }
```

So the rule is the opposite of the folk version: hyphens never need quoting,
leading digits always do. Version numbers as keys (`"2.31"`, `"1.0"`) hit this
constantly.

## "`daemon` and `local` mean 'wherever my store is'"

**Truth: without a `root` parameter they refer to a compiled-in value, `/`.**
`local` and `daemon` are not resolved from the environment or from where you
happen to be -- they name the store rooted at the compile-time prefix.

To talk about a store somewhere else, say so:
`local://?root=/path` , or `unix:///path/to/socket?root=/path`.

**Why it matters here:** a test that opens `local` believing it has an
isolated store is talking to the real system store. Every store fixture in
this repo passes an explicit `root=` for exactly that reason (see
`nanopynix_testing.nix_environment`), and a store path is not executable at the
path it reports when the store is relocated.

## "One process can hold as many `LocalStore` objects as it likes"

**Truth: Nix assumes one for each process, and says so in a comment.**
`LocalStore` names its temp-roots lock file `<stateDir>/temproots/<getpid()>`,
and `LocalStore::createTempRootsFile` (`src/libstore/gc.cc`) removes a file
that is already there:

```c++
    if (pathExists(fnTempRoots))
        /* It *must* be stale, since there can be no two
           processes with the same pid. */
        tryUnlink(fnTempRoots);
```

The process id is the identity of the store. A second `LocalStore` in one
process breaks that in two ways, and which one a run gets is a race between
`pathExists` and `openLockFile`:

- Both open the same inode. The first one takes a waiting exclusive `flock`
  and holds it for the life of the store, so the second one waits for ever.
  `lockFile` looks for an interrupt only after `flock` returns, so nothing
  cancels the wait.
- They do not share the inode. The second one then removes the file of the
  first one, and the temporary roots of the first store are invisible to the
  garbage collector.

The first failure is a deadlock, and the second is a path that disappears
while a store is using it.

`nix::openStore` keeps **no cache**, so every call makes a new store. That is
right for the `nix` command, which opens one store and exits. It is not right
for a library.

**Nix removed the assumption after 2.35.** On `master` the name comes from
`makeTempPath(tempRootsDir, "temproots")`, so each store gets its own file and
neither failure above can happen:

```c++
// 2.34 and 2.35, src/libstore/local-store.cc
, fnTempRoots(tempRootsDir / std::to_string(getpid()))
// master
, fnTempRoots(makeTempPath(tempRootsDir, "temproots"))
```

The supported floor is 2.34, so the two released versions still carry it. Do
not read a test that passes on `git` as proof that the code is safe: the
failure is version-specific, and `git` is the version that cannot show it.

**Why it matters here:** `nanopynix-bindings` keeps one `LocalStore` for each
state directory, in `nix_store.cpp`. Do not remove that cache, and do not key
it on the store URI: a URI names a location, and pytest gives one location to
two different directories in one session. Issue #99.

## Adding to this file

Only add things that are *true* and *surprising* -- a rule someone competent
would get wrong, not a rule someone new has not read yet. Check the claim
against a real Nix and paste the output; the value of this file is that it can
be trusted without re-deriving it.
