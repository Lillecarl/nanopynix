# Configuration

`pynix` reads a default for the options that repeat across the commands, so a
profile does not have to state them on each invocation.

## Where a value comes from

Four layers. The first one that names a value wins:

1. the flag on the command line
2. the environment
3. `$XDG_CONFIG_HOME/pynix/config.toml`
4. the built-in default

```console
$ pynix build --store local ...        # the flag
$ PYNIX_STORE=daemon pynix build ...   # the environment
```

`XDG_CONFIG_HOME` defaults to `~/.config`. `PYNIX_CONFIG` names another file,
for a user who keeps more than one profile. A file that is not there is not an
error.

## The file

```toml
[defaults]
store = "daemon"
verbosity = "notice"
print-build-logs = true

[nix]
substituters = ["https://cache.nixos.org/", "https://mine.example/"]
trusted-public-keys = ["cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="]
max-jobs = 8
```

`[defaults]` holds the options of `pynix` itself. Each key is the option
without the leading dashes, and the environment variable is `PYNIX_` with the
name in capitals: `store` is `PYNIX_STORE`.

`[nix]` holds the Nix settings, under the names that `nix.conf` uses. The
environment variable is `PYNIX_NIX_` with the name in capitals: `max-jobs` is
`PYNIX_NIX_MAX_JOBS`.

A setting that takes more than one value takes a TOML array, and it also takes
the `nix.conf` spelling:

```toml
[nix]
substituters = "https://cache.nixos.org/ https://mine.example/"
```

The second spelling is the one that an environment variable needs, because a
variable carries one string:

```console
$ export PYNIX_NIX_SUBSTITUTERS='https://cache.nixos.org/ https://mine.example/'
$ export PYNIX_NIX_ACCESS_TOKENS='github.com=<token>'
```

## Turning off an option that the file turns on

`print-build-logs` is a flag, so a file that sets it to `true` would leave no
way to turn it off. Each such option has a negative form:

```console
$ pynix build --no-print-build-logs ...
```

## What `pynix build` does not need

`nix build` writes a `result` symlink, which is a GC root, and it prints
nothing unless it is told to. `pynix build` creates no symlink and no root,
and it prints the outputs as JSON. There is no `--no-link` to configure,
because there is no link.

To make a root, ask for one: `pynix store add-root <path> <link>`.
