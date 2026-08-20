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

## The two variables a completion reads

A Tab evaluates Nix, so it has a budget. These two are read from the
environment alone: they are not options of the command, they have no entry in
the configuration file, and they do not go through the settings model. A
completion runs on every keypress that ends in Tab, and a settings tree costs
more than the number is worth.

| variable | default | what it does |
| --- | --- | --- |
| `PYNIX_COMPLETION_BUDGET` | `5.0` | Seconds a completion may take. When it runs out, the completion offers nothing, and the shell shows what it shows when no program answers. |
| `PYNIX_COMPLETION_DEBUG` | unset | A file name. A completion that fails writes its traceback there. |

Raise the budget when you complete against something large and you would
rather wait:

```console
$ export PYNIX_COMPLETION_BUDGET=15
```

**A completion that answers nothing looks the same whatever went wrong.** That
is deliberate -- a traceback would land in the middle of your command line --
and it means a defect here is invisible. `PYNIX_COMPLETION_DEBUG` is the way
to look.

**The budget does not stop a flake input that never answers.** The fetch runs
below the layer that the budget cancels, so a flake with an unreachable input
outlasts it. Issue #231 holds that.

## What a Tab for `--flake` reads

Before the `#`, `--flake` completes a flake reference, and it reads the same
three sources `nix` reads: the bare `.`, the directories under what you have
typed, and every layer of the flake registry.

**The registry can download.** The global layer names a URL in the
`flake-registry` setting, and Nix fetches it. That is what `nix` does on the
same keypress, and the result is cached for `tarball-ttl` seconds, so only the
first Tab of an hour pays for it. Measured: 0.54 s warm, 4.10 s with no
network and an expired cache, and Nix answers from the stale copy in that case
rather than failing.

**A Tab downloads once, where a command downloads five times.** Nix retries a
download `download-attempts` times with a backoff, and waits `connect-timeout`
seconds for each. The defaults are 5 and 15 s, and both outlast a keypress. A
completion sets them to 1 and 3 s. Measured with no network: the registry call
gives up after 4.646 s at the defaults and after 0.002 s at one attempt, and
the first figure is over the budget. Only a completion reads these values; a
real command keeps the patient ones.

**A Tab still completes with no network, and under `nix` it does not.**
`getRegistries` builds all four registry layers before it returns any of them,
so `nix` throws away your `/etc/nix/registry.json` and your own
`registry.json` whenever it cannot reach the global one. Measured on a machine
that pins `nixpkgs` in its system registry: `nix build nixp<TAB>` with an
unreachable registry offers nothing at all. `pynix` asks again without the
global layer and offers what the local ones hold.

**`flake-registry` in your `nix.conf` does not reach this program.** Nix
registers its one global fetch-settings object with `globalConfig`, and
`nix.conf` fills that object in. nanopynix builds a fetch-settings object of
its own for each call and registers none of them, so every fetch setting comes
from the caller and none from the file. Measured: `flake-registry` is absent
from `nanopynix.list_settings()`, which is `globalConfig` itself. Issue #234
holds that gap. The layer is on here because it is Nix's own default, and not
because the file said so.

## What `pynix build` does not need

`nix build` writes a `result` symlink, which is a GC root, and it prints
nothing unless it is told to. `pynix build` creates no symlink and no root,
and it prints the outputs as JSON. There is no `--no-link` to configure,
because there is no link.

To make a root, ask for one: `pynix store add-root <path> <link>`.
