# pynix

`pynix` is a command-line tool for Nix built on top of `nanopynix` — store
inspection and garbage collection, derivation/flake evaluation, and build
orchestration, all through nanopynix's worker instead of shelling out to
`nix`.

```{toctree}
:maxdepth: 2
:caption: Contents

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
arguments, and defaults.
