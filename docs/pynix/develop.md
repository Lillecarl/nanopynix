# Development environments

`pynix develop` and `pynix print-dev-env` give a package's build environment.
They are the counterparts of `nix develop` and `nix print-dev-env`, and they
compute the same environment by the same steps.

## `pynix develop`

Without a command, `develop` starts an interactive bash in the environment:

```console
$ pynix develop --file default.nix --attr hello
$ pynix develop --flake .#hello
```

The shell reads your `~/.bashrc` first, so your prompt and your aliases
survive. The build environment is applied over it.

**Everything after `--` is the command.** This is the one difference from
`nix develop`, which uses a `--command` option:

```console
$ pynix develop --flake .# -- make -j4
$ pynix develop --file default.nix --attr hello -- ./configure --prefix=/tmp/x
```

No option after `--` is interpreted by `pynix`, so `-j4` and `--prefix=/tmp/x`
above reach the command unchanged. The command replaces `pynix`, so its exit
status is the exit status of the whole invocation.

A pipeline is the shell's, and not the command's. Write the pipeline inside a
shell that runs in the environment:

```console
$ pynix develop --flake .# -- bash -c 'make | less'
```

## `pynix print-dev-env`

`print-dev-env` computes the same environment and prints it. With no option it
prints bash that restores the environment, which you can source:

```console
$ pynix print-dev-env --flake .#hello > env.sh
$ source env.sh
```

With `--json` it prints the environment as a document: each variable with its
kind (`exported`, `var`, `array` or `associative`), every bash function, and
the structured attributes of a `__structuredAttrs = true` derivation.

```console
$ pynix print-dev-env --flake .#hello --json
```

## How it works

Nix computes a build environment in six steps, in `src/nix/develop.cc`, and
`pynix` does the same six:

1. Read the derivation.
2. Refuse a derivation whose builder is not `bash`.
3. Rewrite the derivation so that its builder runs `get-env.sh`, which dumps
   the environment as JSON.
4. Write the rewritten derivation to the store.
5. Build it.
6. Read the JSON that the builder wrote.

`pynix` performs steps 1 to 4 in one library call,
`Store.write_dev_shell_derivation`. The three supported Nix versions disagree
about how a derivation is written and how its output paths are filled, so that
part lives in the C++ bindings with the rest of the version handling.

`get-env.sh` is Nix's own `src/nix/get-env.sh`. Nix compiles it into the
`nix` binary and no library carries it, so the bindings carry it
(LGPL-2.1-or-later). The Nix build copies it from the Nix source of the
version it links, so the `-env` derivation that `pynix` builds is the same one
that `nix` builds.

The original derivation is not changed. The rewrite writes a second one.

## What is not supported yet

`--profile`, `--redirect`, `--unpack`, `--configure`, `--build`, `--install`
and `--phase` have no counterpart yet. `--command` is deliberately absent:
`--` replaces it.
