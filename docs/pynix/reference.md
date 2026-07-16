# CLI reference

Generated from pynix's live `clypi` command tree — see `docs/_generate_pynix_reference.py`. Every command also accepts `--help` for the same information at the terminal.

## `pynix`

pynix — nanopynix CLI

### `pynix build`

Build a Nix derivation value

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `--file` | `Path or None` | `None` | Evaluate FILE as a Nix expression. |
| `--attr` | `str or None` | `None` | Dot-separated attribute path within the evaluation result. |
| `--flake` | `str or None` | `None` | Evaluate FLAKE, optionally with a '#'-separated attribute path. |
| `--store` | `str` | `'auto'` | Store URI to build with. |
| `--eval-store` | `str or None` | `None` | Store URI to evaluate with. Defaults to --store. |
| `--substituters` | `str` | `'https://cache.nixos.org/'` | Space-separated substituter URLs. |
| `--trusted-public-keys` | `str` | `'cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY='` | Space-separated substituter public keys. |
| `--verbosity` | `str` | `'notice'` | Nix log verbosity: error, warn, notice, info, talkative, chatty, debug, vomit, or 0-7. |
| `--print-build-logs` | `bool` | `False` | Print build log lines to stderr. |

### `pynix config`

Inspect Nix configuration

#### `pynix config show`

Show Nix configuration settings

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `--setting` | `str or None` | `None` | Show only one setting. |

#### `pynix config check`

Check that Nix configuration can be loaded

#### `pynix config current-system`

Show the effective system used by builtins.currentSystem

### `pynix eval`

Evaluate a Nix expression and print the result as JSON

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `--expr` | `str or None` | `None` | Nix expression to evaluate. Reads from stdin if not provided. |
| `--file` | `Path or None` | `None` | Evaluate FILE as a Nix expression. |
| `--attr` | `str or None` | `None` | Dot-separated attribute path within the evaluation result. |
| `--flake` | `str or None` | `None` | Evaluate FLAKE, optionally with a '#'-separated attribute path. |
| `--store` | `str` | `'auto'` | Store URI to evaluate with. |

### `pynix derivation`

Inspect and manipulate Nix derivations

#### `pynix derivation show`

Show the contents of a Nix derivation

Examples:
  pynix derivation show --file default.nix --attr hello
  pynix derivation show --flake .#hello
  pynix derivation show --flake nixpkgs#python3Packages.requests

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `--file` | `Path or None` | `None` | Evaluate FILE as a Nix expression. |
| `--attr` | `str or None` | `None` | Dot-separated attribute path within the evaluation result. |
| `--flake` | `str or None` | `None` | Evaluate FLAKE, optionally with a '#'-separated attribute path. |
| `--store` | `str` | `'auto'` | Store URI to use. |

### `pynix flake`

Inspect and manage Nix flakes

#### `pynix flake show`

Show the outputs provided by a flake

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `flake-ref` | `str` | *required* | Flake reference (e.g. '.#' or 'nixpkgs#'). |
| `--attrpath` | `str or None` | `None` | Dot-separated attribute path within the flake outputs to start from. |
| `--store` | `str` | `'auto'` | Store URI to evaluate with. |

#### `pynix flake metadata`

Show locked flake metadata

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `flake-ref` | `str` | *required* | Flake reference (e.g. '.' or 'nixpkgs'). |
| `--store` | `str` | `'auto'` | Store URI to evaluate with. |

#### `pynix flake info`

Alias for flake metadata

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `flake-ref` | `str` | *required* | Flake reference (e.g. '.' or 'nixpkgs'). |
| `--store` | `str` | `'auto'` | Store URI to evaluate with. |

### `pynix log`

Show the build log for a store path

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `path` | `str` | *required* | Store path whose build log should be printed. |
| `--store` | `str` | `'auto'` | Store URI to query. |

### `pynix path-info`

Show information about a store path

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `path` | `str` | *required* | Store path to query (e.g. '/nix/store/hash-name'). |
| `--store` | `str` | `'auto'` | Store URI to query. |

### `pynix repl`

Open an interactive Nix evaluation session.

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `--store` | `str` | `'auto'` | Store URI to evaluate with. |
| `--file` | `Path or None` | `None` | Evaluate FILE as a Nix expression. |
| `--attr` | `str or None` | `None` | Dot-separated attribute path within the evaluation result. |
| `--flake` | `str or None` | `None` | Evaluate FLAKE, optionally with a '#'-separated attribute path. |

### `pynix store`

Manage the Nix store

#### `pynix store gc`

Manage Nix store garbage collection

##### `pynix store gc print-roots`

List the garbage collector roots

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `--store` | `str` | `'auto'` | Store URI to query. |

##### `pynix store gc print-dead`

List paths that would be removed by a garbage collection.
Use --rip to actually delete them.

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `--rip` | `bool` | `False` | Actually delete the dead store paths instead of just listing them. |
| `--store` | `str` | `'auto'` | Store URI to query. |

##### `pynix store gc print-alive`

List live paths in the store (reachable from GC roots)

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `--store` | `str` | `'auto'` | Store URI to query. |

#### `pynix store info`

Show store metadata

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `--store` | `str` | `'auto'` | Store URI to query. |

#### `pynix store dirs`

Show configured local store directories

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `--store` | `str` | `'auto'` | Store URI to query. |

#### `pynix store is-valid-path`

Check whether a store path is valid

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `path` | `str` | *required* | Store path to check. |
| `--store` | `str` | `'auto'` | Store URI to query. |

#### `pynix store follow-links-to-store-path`

Resolve symlinks to a store path

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `path` | `str` | *required* | Filesystem path to resolve. |
| `--store` | `str` | `'auto'` | Store URI to query. |

#### `pynix store compute-fs-closure`

Compute the filesystem closure of a store path

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `path` | `str` | *required* | Store path to query. |
| `--flip-direction` | `bool` | `False` | Compute the inverse closure. |
| `--include-outputs` | `bool` | `False` | Include derivation outputs. |
| `--include-derivers` | `bool` | `False` | Include derivers. |
| `--store` | `str` | `'auto'` | Store URI to query. |

#### `pynix store query-missing`

Show which paths would need building, substituting, or are unknown

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `paths` | `list[str]` | *required* | Store paths to query. |
| `--store` | `str` | `'auto'` | Store URI to query. |

#### `pynix store query-derivation-outputs`

Show the outputs of a derivation path

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `path` | `str` | *required* | Derivation path to query. |
| `--store` | `str` | `'auto'` | Store URI to query. |

#### `pynix store query-valid-derivers`

Show valid derivers for a store path

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `path` | `str` | *required* | Store path to query. |
| `--store` | `str` | `'auto'` | Store URI to query. |

#### `pynix store list-valid-paths`

List all valid paths in the store

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `--store` | `str` | `'auto'` | Store URI to query. |

#### `pynix store query-referrers`

Show referrers of a store path

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `path` | `str` | *required* | Store path to query. |
| `--store` | `str` | `'auto'` | Store URI to query. |

#### `pynix store query-substitutable-paths`

Show which paths are substitutable

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `paths` | `list[str]` | *required* | Store paths to query. |
| `--store` | `str` | `'auto'` | Store URI to query. |

#### `pynix store add-temp-root`

Add a temporary GC root for this command's store session

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `path` | `str` | *required* | Store path to root temporarily. |
| `--store` | `str` | `'auto'` | Store URI to use. |

#### `pynix store add-perm-root`

Add a permanent GC root symlink

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `path` | `str` | *required* | Store path to root. |
| `gc-root` | `str` | *required* | GC root symlink to create. |
| `--store` | `str` | `'auto'` | Store URI to use. |

#### `pynix store add-indirect-root`

Register an indirect GC root

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `path` | `str` | *required* | GC root path to register. |
| `--store` | `str` | `'auto'` | Store URI to use. |

#### `pynix store path-from-hash-part`

Resolve a store path from its hash prefix

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `hash-part` | `str` | *required* | Store path hash prefix to resolve. |
| `--store` | `str` | `'auto'` | Store URI to query. |

#### `pynix store ensure-path`

Ensure a store path is valid, substituting it if available

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `path` | `str` | *required* | Store path to ensure. |
| `--store` | `str` | `'auto'` | Store URI to use. |

#### `pynix store cat`

Print a file inside a local Nix store path

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `path` | `str` | *required* | File path to print. |
| `--store` | `str` | `'auto'` | Store URI to use. |

#### `pynix store ls`

List files inside a local Nix store path

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `path` | `str` | *required* | File or directory path to list. |
| `--json` | `bool` | `False` | Print machine-readable JSON. |
| `--store` | `str` | `'auto'` | Store URI to use. |

#### `pynix store add`

Add a file or directory to a Nix store

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `path` | `str` | *required* | Filesystem path to add. |
| `--name` | `str or None` | `None` | Override the store path name component. |
| `--mode` | `str` | `'nar'` | Content-addressing method: nar, flat, or git. |
| `--hash-algo` | `str` | `'sha256'` | Hash algorithm to use. |
| `--dry-run` | `bool` | `False` | Compute the store path without adding the content. |
| `--store` | `str` | `'auto'` | Store URI to use. |

#### `pynix store add-file`

Add a single file to a Nix store

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `path` | `str` | *required* | Filesystem path to add. |
| `--name` | `str or None` | `None` | Override the store path name component. |
| `--hash-algo` | `str` | `'sha256'` | Hash algorithm to use. |
| `--dry-run` | `bool` | `False` | Compute the store path without adding the content. |
| `--store` | `str` | `'auto'` | Store URI to use. |

#### `pynix store add-path`

Add a path to a Nix store using NAR ingestion

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `path` | `str` | *required* | Filesystem path to add. |
| `--name` | `str or None` | `None` | Override the store path name component. |
| `--hash-algo` | `str` | `'sha256'` | Hash algorithm to use. |
| `--dry-run` | `bool` | `False` | Compute the store path without adding the content. |
| `--store` | `str` | `'auto'` | Store URI to use. |

#### `pynix store diff-closures`

Compare two filesystem closures

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `before` | `str` | *required* | Original store path. |
| `after` | `str` | *required* | New store path. |
| `--store` | `str` | `'auto'` | Store URI to query. |

#### `pynix store optimise`

Optimise store disk usage by hard-linking duplicate files

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `--store` | `str` | `'auto'` | Store URI to optimise. |

#### `pynix store verify`

Verify store integrity

| Argument | Type | Default | Help |
| --- | --- | --- | --- |
| `--check-contents` | `bool` | `False` | Check path contents, not only metadata. |
| `--repair` | `bool` | `False` | Attempt repair while verifying. |
| `--store` | `str` | `'auto'` | Store URI to verify. |

