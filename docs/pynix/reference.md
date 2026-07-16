# CLI reference

Generated from pynix's live `clypi` command tree — see `docs/_generate_pynix_reference.py`. Every command also accepts `--help` for the same information at the terminal.

## `pynix`

pynix — nanopynix CLI

### `pynix build`

Build a Nix derivation value

| Argument | Type | Help |
| --- | --- | --- |
| `--file` | `Path or None` | Evaluate FILE as a Nix expression. (default: `None`) |
| `--attr` | `str or None` | Dot-separated attribute path within the evaluation result. (default: `None`) |
| `--flake` | `str or None` | Evaluate FLAKE, optionally with a '#'-separated attribute path. (default: `None`) |
| `--store` | `str` | Store URI to build with. (default: `'auto'`) |
| `--eval-store` | `str or None` | Store URI to evaluate with. Defaults to --store. (default: `None`) |
| `--substituters` | `str` | Space-separated substituter URLs. (default: `'https://cache.nixos.org/'`) |
| `--trusted-public-keys` | `str` | Space-separated substituter public keys. (default: `'cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY='`) |
| `--verbosity` | `str` | Nix log verbosity: error, warn, notice, info, talkative, chatty, debug, vomit, or 0-7. (default: `'notice'`) |
| `--print-build-logs` | `bool` | Print build log lines to stderr. (default: `False`) |

### `pynix config`

Inspect Nix configuration

#### `pynix config show`

Show Nix configuration settings

| Argument | Type | Help |
| --- | --- | --- |
| `--setting` | `str or None` | Show only one setting. (default: `None`) |

#### `pynix config check`

Check that Nix configuration can be loaded

#### `pynix config current-system`

Show the effective system used by builtins.currentSystem

### `pynix eval`

Evaluate a Nix expression and print the result as JSON

| Argument | Type | Help |
| --- | --- | --- |
| `--expr` | `str or None` | Nix expression to evaluate. Reads from stdin if not provided. (default: `None`) |
| `--file` | `Path or None` | Evaluate FILE as a Nix expression. (default: `None`) |
| `--attr` | `str or None` | Dot-separated attribute path within the evaluation result. (default: `None`) |
| `--flake` | `str or None` | Evaluate FLAKE, optionally with a '#'-separated attribute path. (default: `None`) |
| `--store` | `str` | Store URI to evaluate with. (default: `'auto'`) |

### `pynix derivation`

Inspect and manipulate Nix derivations

#### `pynix derivation show`

Show the contents of a Nix derivation

Examples:
  pynix derivation show --file default.nix --attr hello
  pynix derivation show --flake .#hello
  pynix derivation show --flake nixpkgs#python3Packages.requests

| Argument | Type | Help |
| --- | --- | --- |
| `--file` | `Path or None` | Evaluate FILE as a Nix expression. (default: `None`) |
| `--attr` | `str or None` | Dot-separated attribute path within the evaluation result. (default: `None`) |
| `--flake` | `str or None` | Evaluate FLAKE, optionally with a '#'-separated attribute path. (default: `None`) |
| `--store` | `str` | Store URI to use. (default: `'auto'`) |

### `pynix flake`

Inspect and manage Nix flakes

#### `pynix flake show`

Show the outputs provided by a flake

| Argument | Type | Help |
| --- | --- | --- |
| `flake-ref` | `str` | Flake reference (e.g. '.#' or 'nixpkgs#'). *(required)* |
| `--attrpath` | `str or None` | Dot-separated attribute path within the flake outputs to start from. (default: `None`) |
| `--store` | `str` | Store URI to evaluate with. (default: `'auto'`) |

#### `pynix flake metadata`

Show locked flake metadata

| Argument | Type | Help |
| --- | --- | --- |
| `flake-ref` | `str` | Flake reference (e.g. '.' or 'nixpkgs'). *(required)* |
| `--store` | `str` | Store URI to evaluate with. (default: `'auto'`) |

#### `pynix flake info`

Alias for flake metadata

| Argument | Type | Help |
| --- | --- | --- |
| `flake-ref` | `str` | Flake reference (e.g. '.' or 'nixpkgs'). *(required)* |
| `--store` | `str` | Store URI to evaluate with. (default: `'auto'`) |

### `pynix log`

Show the build log for a store path

| Argument | Type | Help |
| --- | --- | --- |
| `path` | `str` | Store path whose build log should be printed. *(required)* |
| `--store` | `str` | Store URI to query. (default: `'auto'`) |

### `pynix path-info`

Show information about a store path

| Argument | Type | Help |
| --- | --- | --- |
| `path` | `str` | Store path to query (e.g. '/nix/store/hash-name'). *(required)* |
| `--store` | `str` | Store URI to query. (default: `'auto'`) |

### `pynix repl`

Open an interactive Nix evaluation session.

| Argument | Type | Help |
| --- | --- | --- |
| `--store` | `str` | Store URI to evaluate with. (default: `'auto'`) |
| `--file` | `Path or None` | Evaluate FILE as a Nix expression. (default: `None`) |
| `--attr` | `str or None` | Dot-separated attribute path within the evaluation result. (default: `None`) |
| `--flake` | `str or None` | Evaluate FLAKE, optionally with a '#'-separated attribute path. (default: `None`) |

### `pynix store`

Manage the Nix store

#### `pynix store gc`

Manage Nix store garbage collection

##### `pynix store gc print-roots`

List the garbage collector roots

| Argument | Type | Help |
| --- | --- | --- |
| `--store` | `str` | Store URI to query. (default: `'auto'`) |

##### `pynix store gc print-dead`

List paths that would be removed by a garbage collection.
Use --rip to actually delete them.

| Argument | Type | Help |
| --- | --- | --- |
| `--rip` | `bool` | Actually delete the dead store paths instead of just listing them. (default: `False`) |
| `--store` | `str` | Store URI to query. (default: `'auto'`) |

##### `pynix store gc print-alive`

List live paths in the store (reachable from GC roots)

| Argument | Type | Help |
| --- | --- | --- |
| `--store` | `str` | Store URI to query. (default: `'auto'`) |

#### `pynix store info`

Show store metadata

| Argument | Type | Help |
| --- | --- | --- |
| `--store` | `str` | Store URI to query. (default: `'auto'`) |

#### `pynix store dirs`

Show configured local store directories

| Argument | Type | Help |
| --- | --- | --- |
| `--store` | `str` | Store URI to query. (default: `'auto'`) |

#### `pynix store is-valid-path`

Check whether a store path is valid

| Argument | Type | Help |
| --- | --- | --- |
| `path` | `str` | Store path to check. *(required)* |
| `--store` | `str` | Store URI to query. (default: `'auto'`) |

#### `pynix store follow-links-to-store-path`

Resolve symlinks to a store path

| Argument | Type | Help |
| --- | --- | --- |
| `path` | `str` | Filesystem path to resolve. *(required)* |
| `--store` | `str` | Store URI to query. (default: `'auto'`) |

#### `pynix store compute-fs-closure`

Compute the filesystem closure of a store path

| Argument | Type | Help |
| --- | --- | --- |
| `path` | `str` | Store path to query. *(required)* |
| `--flip-direction` | `bool` | Compute the inverse closure. (default: `False`) |
| `--include-outputs` | `bool` | Include derivation outputs. (default: `False`) |
| `--include-derivers` | `bool` | Include derivers. (default: `False`) |
| `--store` | `str` | Store URI to query. (default: `'auto'`) |

#### `pynix store query-missing`

Show which paths would need building, substituting, or are unknown

| Argument | Type | Help |
| --- | --- | --- |
| `paths` | `list[str]` | Store paths to query. *(required)* |
| `--store` | `str` | Store URI to query. (default: `'auto'`) |

#### `pynix store query-derivation-outputs`

Show the outputs of a derivation path

| Argument | Type | Help |
| --- | --- | --- |
| `path` | `str` | Derivation path to query. *(required)* |
| `--store` | `str` | Store URI to query. (default: `'auto'`) |

#### `pynix store query-valid-derivers`

Show valid derivers for a store path

| Argument | Type | Help |
| --- | --- | --- |
| `path` | `str` | Store path to query. *(required)* |
| `--store` | `str` | Store URI to query. (default: `'auto'`) |

#### `pynix store list-valid-paths`

List all valid paths in the store

| Argument | Type | Help |
| --- | --- | --- |
| `--store` | `str` | Store URI to query. (default: `'auto'`) |

#### `pynix store query-referrers`

Show referrers of a store path

| Argument | Type | Help |
| --- | --- | --- |
| `path` | `str` | Store path to query. *(required)* |
| `--store` | `str` | Store URI to query. (default: `'auto'`) |

#### `pynix store query-substitutable-paths`

Show which paths are substitutable

| Argument | Type | Help |
| --- | --- | --- |
| `paths` | `list[str]` | Store paths to query. *(required)* |
| `--store` | `str` | Store URI to query. (default: `'auto'`) |

#### `pynix store add-temp-root`

Add a temporary GC root for this command's store session

| Argument | Type | Help |
| --- | --- | --- |
| `path` | `str` | Store path to root temporarily. *(required)* |
| `--store` | `str` | Store URI to use. (default: `'auto'`) |

#### `pynix store add-perm-root`

Add a permanent GC root symlink

| Argument | Type | Help |
| --- | --- | --- |
| `path` | `str` | Store path to root. *(required)* |
| `gc-root` | `str` | GC root symlink to create. *(required)* |
| `--store` | `str` | Store URI to use. (default: `'auto'`) |

#### `pynix store add-indirect-root`

Register an indirect GC root

| Argument | Type | Help |
| --- | --- | --- |
| `path` | `str` | GC root path to register. *(required)* |
| `--store` | `str` | Store URI to use. (default: `'auto'`) |

#### `pynix store path-from-hash-part`

Resolve a store path from its hash prefix

| Argument | Type | Help |
| --- | --- | --- |
| `hash-part` | `str` | Store path hash prefix to resolve. *(required)* |
| `--store` | `str` | Store URI to query. (default: `'auto'`) |

#### `pynix store ensure-path`

Ensure a store path is valid, substituting it if available

| Argument | Type | Help |
| --- | --- | --- |
| `path` | `str` | Store path to ensure. *(required)* |
| `--store` | `str` | Store URI to use. (default: `'auto'`) |

#### `pynix store cat`

Print a file inside a local Nix store path

| Argument | Type | Help |
| --- | --- | --- |
| `path` | `str` | File path to print. *(required)* |
| `--store` | `str` | Store URI to use. (default: `'auto'`) |

#### `pynix store ls`

List files inside a local Nix store path

| Argument | Type | Help |
| --- | --- | --- |
| `path` | `str` | File or directory path to list. *(required)* |
| `--json` | `bool` | Print machine-readable JSON. (default: `False`) |
| `--store` | `str` | Store URI to use. (default: `'auto'`) |

#### `pynix store add`

Add a file or directory to a Nix store

| Argument | Type | Help |
| --- | --- | --- |
| `path` | `str` | Filesystem path to add. *(required)* |
| `--name` | `str or None` | Override the store path name component. (default: `None`) |
| `--mode` | `str` | Content-addressing method: nar, flat, or git. (default: `'nar'`) |
| `--hash-algo` | `str` | Hash algorithm to use. (default: `'sha256'`) |
| `--dry-run` | `bool` | Compute the store path without adding the content. (default: `False`) |
| `--store` | `str` | Store URI to use. (default: `'auto'`) |

#### `pynix store add-file`

Add a single file to a Nix store

| Argument | Type | Help |
| --- | --- | --- |
| `path` | `str` | Filesystem path to add. *(required)* |
| `--name` | `str or None` | Override the store path name component. (default: `None`) |
| `--hash-algo` | `str` | Hash algorithm to use. (default: `'sha256'`) |
| `--dry-run` | `bool` | Compute the store path without adding the content. (default: `False`) |
| `--store` | `str` | Store URI to use. (default: `'auto'`) |

#### `pynix store add-path`

Add a path to a Nix store using NAR ingestion

| Argument | Type | Help |
| --- | --- | --- |
| `path` | `str` | Filesystem path to add. *(required)* |
| `--name` | `str or None` | Override the store path name component. (default: `None`) |
| `--hash-algo` | `str` | Hash algorithm to use. (default: `'sha256'`) |
| `--dry-run` | `bool` | Compute the store path without adding the content. (default: `False`) |
| `--store` | `str` | Store URI to use. (default: `'auto'`) |

#### `pynix store diff-closures`

Compare two filesystem closures

| Argument | Type | Help |
| --- | --- | --- |
| `before` | `str` | Original store path. *(required)* |
| `after` | `str` | New store path. *(required)* |
| `--store` | `str` | Store URI to query. (default: `'auto'`) |

#### `pynix store optimise`

Optimise store disk usage by hard-linking duplicate files

| Argument | Type | Help |
| --- | --- | --- |
| `--store` | `str` | Store URI to optimise. (default: `'auto'`) |

#### `pynix store verify`

Verify store integrity

| Argument | Type | Help |
| --- | --- | --- |
| `--check-contents` | `bool` | Check path contents, not only metadata. (default: `False`) |
| `--repair` | `bool` | Attempt repair while verifying. (default: `False`) |
| `--store` | `str` | Store URI to verify. (default: `'auto'`) |

