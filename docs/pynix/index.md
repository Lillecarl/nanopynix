# pynix

`pynix` is a command-line tool for Nix built on top of `nanopynix` — store
inspection and garbage collection, derivation/flake evaluation, and build
orchestration, all through nanopynix's worker instead of shelling out to
`nix`.

```{toctree}
:maxdepth: 2
:caption: Contents

configuration
develop
reference
```

## Quick start

```console
$ pynix eval --expr '1 + 1'
2

$ pynix eval --flake .#hello --attr version

$ pynix build --flake .#hello

$ pynix develop --flake .#hello

$ pynix develop --flake .#hello -- make -j4

$ pynix flake show .

$ pynix store info

$ pynix store gc print-roots
```

Every command accepts `--help` for its full option list, or see the
{doc}`generated command reference <reference>` for every subcommand, its
arguments, and defaults. To set a default once instead of on each
invocation, see {doc}`configuration`.

## Naming a target

`--file` takes a path, a `<name>` lookup path, a URL, or a flake reference,
and the reference may end with `#` and an attribute path:

```console
$ pynix eval --file ./default.nix#lib.version
$ pynix eval --file github:NixOS/nixpkgs/nixos-25.05#lib.version
```

A reference that no local path matches is fetched, and the file inside the
fetched tree is evaluated as an ordinary Nix file. This does not read
`flake.nix`, which is what `--flake` reads.

`--flake` resolves its `#` fragment the way the `nix` CLI resolves one. The
fragment is not one attribute path: each command holds a list of prefixes,
and the first path that resolves is the answer. `pynix build --flake
nixpkgs#hello` therefore tries `packages.<system>.hello`, then
`legacyPackages.<system>.hello`, and then `hello`.

Three rules follow:

- **A command decides its own prefixes.** `pynix develop` looks in
  `devShells.<system>.` first, and `pynix repl` starts at the outputs
  themselves.
- **No fragment takes a default.** `pynix build --flake .` builds
  `packages.<system>.default`, and `pynix develop --flake .` enters
  `devShells.<system>.default`.
- **A leading `.` turns every prefix off.** `--flake '.#.hello'` reads the
  output named `hello` at the top level, which a prefix would otherwise hide.

`--attr` is different, and it stays different: it is one exact attribute
path, applied to whatever the target resolved to.
