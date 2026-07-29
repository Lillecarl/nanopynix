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
| `--verbosity` | `str or None` | Nix log verbosity: error, warn, notice, info, talkative, chatty, debug, vomit, or 0-7. (default: `None`) |
| `--print-build-logs` | `bool` | Print build log lines to stderr. (default: `False`) |
| `--update-fod` | `bool` | Update plain fixed-output hash literals after a hash mismatch. (default: `False`) |
| `--dry-run` | `bool` | Show --update-fod changes without writing or rebuilding. (default: `False`) |
| `--namespaced` | `bool` | Build in a private user namespace, against an overlay store whose lower layer is the host store. Nothing is copied in, the host store does not change, and this process owns the sandbox settings that the daemon otherwise controls. Linux only. (default: `False`) |
| `--overlay-dir` | `Path or None` | Keep the overlay's upper layer here, instead of in a temporary directory that is deleted on exit. Reuse the same directory to keep what earlier --namespaced builds produced. Implies --namespaced. (default: `None`) |
| `--copy-back` | `bool` | Copy the outputs of a --namespaced build into the host store when the build succeeds. Without it the outputs are gone when the worker exits. (default: `True`) |
| `--sandbox-path` | `list[str]` | Extra path to mount into the build sandbox, as /inside=/outside or /path. Repeatable. Requires --namespaced, because the daemon does not let a client change its sandbox. (default: `[]`) |

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

### `pynix lsp`

Run pynix as a Nix language server (stdio transport).

Files opt in to real hover/completion by naming a bound identifier and a
Nix expression to evaluate in a header comment near the top of the file:

    # pynix-lsp: cfg = (import ./flake.nix).nixosConfigurations.myhost.config.services.foo

Any attribute path in the file rooted at that name (e.g. ``cfg.enable``)
is then resolved through the expression's evaluated value.

### `pynix osearch`

Search NixOS module options, using a cached, offline index.

| Argument | Type | Help |
| --- | --- | --- |
| `query` | `str or None` | Search query. Omit to just (re)build the index. (default: `None`) |
| `--file` | `Path or None` | Evaluate FILE as a Nix expression. (default: `None`) |
| `--attr` | `str or None` | Dot-separated attribute path within the evaluation result. (default: `None`) |
| `--flake` | `str or None` | Evaluate FLAKE, optionally with a '#'-separated attribute path. (default: `None`) |
| `--options-attr` | `str` | Attribute path to the options tree, relative to the target. (default: `'options'`) |
| `--lib-attr` | `str` | Attribute path to nixpkgs lib, relative to the target. (default: `'pkgs.lib'`) |
| `--update-index` | `bool` | Re-evaluate and rebuild the cached index instead of using it. (default: `False`) |
| `--limit` | `int` | Maximum number of results to print. (default: `20`) |
| `--json-output` | `bool` | Print results as JSON instead of a human-readable list. (default: `False`) |
| `--store` | `str` | Store URI to evaluate with. (default: `'auto'`) |

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

### `pynix ekn`

easykubenix CLI — evaluate Nix and manage GitOps release branches.

| Argument | Type | Help |
| --- | --- | --- |
| `--file` | `Path or None` | Nix file to evaluate. (default: `None`) |
| `--flake` | `str or None` | Flake reference (e.g. '.#myconfig'). Evaluates outputs.eknConfig.<system>.<attr>. (default: `None`) |
| `--attr` | `str or None` | Dot-separated attribute path within the evaluation result. (default: `None`) |

#### `pynix ekn deploy`

Verify, push the pre-deploy cache, commit, and push -- the whole
release in one command.

Chains Validate (unless --no-verify) -> cache push (`ekn.cacheTo`/
`ekn.cachePackage`, read straight from Nix config -- see
`evaluate_cache_config`; a no-op if `ekn.cacheTo` is unset) -> Commit
(render + write GitOps branches, git-pushed with --push).

The cache push runs *before* the git commit/push deliberately: ArgoCD/
Flux may sync the instant the branch updates, and CSI-mounted store
paths referenced in the manifests need to already be substitutable at
that point, not eventually -- a failed cache push aborts the deploy by
default (see --cache-allow-failure).

| Argument | Type | Help |
| --- | --- | --- |
| `--file` | `Path or None` |  (default: `None`) |
| `--flake` | `str or None` |  (default: `None`) |
| `--attr` | `str or None` | Dot-separated attribute path within the evaluation result. (default: `None`) |
| `--no-verify` | `bool` | Skip temporary API-server and kubeconform verification. (default: `False`) |
| `--message` | `str or None` | Commit message. (default: `None`) |
| `--push` | `bool` | git push each committed GitOps branch to its remote afterwards -- commit_manifests only ever commits locally, and ArgoCD/Flux read from the remote. (default: `False`) |
| `--remote` | `str` | Remote to push GitOps branches to (with --push). (default: `'origin'`) |
| `--cache-allow-failure` | `bool` | Log a warning and continue if the pre-deploy cache push fails, instead of aborting. Off by default -- CSI-mounted pods will fail to start if referenced store paths were never pushed, so a failed push should normally block the deploy. (default: `False`) |
| `--verbosity` | `str` | Nix log verbosity for every eval/build nanopynix does during this deploy (error, warn, notice, info, talkative, chatty, debug, vomit). `nix run --print-build-logs` only covers building the ekn CLI package itself, not what it does at runtime -- this is the runtime equivalent. (default: `'error'`) |
| `--print-build-logs` | `bool` | Stream build/eval log lines from nanopynix's worker to stderr as they happen, for visibility into what's taking long during Validate/cache-push/Commit. (default: `False`) |

#### `pynix ekn eval`

Evaluate Nix and dump JSON.

| Argument | Type | Help |
| --- | --- | --- |
| `--file` | `Path or None` |  (default: `None`) |
| `--flake` | `str or None` |  (default: `None`) |
| `--attr` | `str or None` | Dot-separated attribute path within the evaluation result. (default: `None`) |
| `--update-fod` | `bool` | On a fixed-output hash mismatch, patch --source-file's plain-string hash literal with Nix's reported hash and retry. (default: `False`) |
| `--source-file` | `Path or None` | Nix file containing the fixed-output hash literal to patch (required with --update-fod). (default: `None`) |

#### `pynix ekn render`

Render Kubernetes manifests as YAML on stdout.

| Argument | Type | Help |
| --- | --- | --- |
| `--file` | `Path or None` |  (default: `None`) |
| `--flake` | `str or None` |  (default: `None`) |
| `--attr` | `str or None` | Dot-separated attribute path within the evaluation result. (default: `None`) |

#### `pynix ekn diff`

Diff GitOps-routed manifests against the deploy branch.

| Argument | Type | Help |
| --- | --- | --- |
| `--file` | `Path or None` |  (default: `None`) |
| `--flake` | `str or None` |  (default: `None`) |
| `--attr` | `str or None` | Dot-separated attribute path within the evaluation result. (default: `None`) |

#### `pynix ekn commit`

Render manifests and write them to the GitOps deploy (and paired
source) branch.

| Argument | Type | Help |
| --- | --- | --- |
| `--file` | `Path or None` |  (default: `None`) |
| `--flake` | `str or None` |  (default: `None`) |
| `--attr` | `str or None` | Dot-separated attribute path within the evaluation result. (default: `None`) |
| `--message` | `str or None` | Commit message. (default: `None`) |
| `--push` | `bool` | git push the committed GitOps branch(es) to their remote afterwards -- commits are only ever made locally, and ArgoCD/Flux read from the remote. (default: `False`) |
| `--remote` | `str` | Remote to push GitOps branch(es) to (with --push). (default: `'origin'`) |

#### `pynix ekn rollback`

Roll back the GitOps deploy (and paired source) branch to an older
commit -- forward-only, replays the old tree as a *new* commit, never
resets or force-pushes anything.

Deliberately supports skipping Nix evaluation entirely via
`--deploy-branch`/`--source-branch`: an incident is often *why* Nix
eval is currently broken, so rollback can't depend on it working.
`--file`/`--flake` is the routine convenience for a one-step-back
during normal testing, when Nix eval is healthy.

| Argument | Type | Help |
| --- | --- | --- |
| `--deploy-branch` | `str or None` | Deploy branch to roll back, bypassing Nix evaluation entirely. Mutually exclusive with --file/--flake. (default: `None`) |
| `--source-branch` | `str or None` | Paired source branch to roll back alongside --deploy-branch (optional). (default: `None`) |
| `--file` | `Path or None` |  (default: `None`) |
| `--flake` | `str or None` |  (default: `None`) |
| `--attr` | `str or None` | Dot-separated attribute path within the evaluation result. (default: `None`) |
| `--to` | `str or None` | Roll back to this specific commit-ish instead of walking --steps-back. (default: `None`) |
| `--steps-back` | `int` | Number of deploy-branch first-parent steps to roll back, when --to is not given. (default: `1`) |
| `--push` | `bool` | git push the rolled-back branch(es) to their remote afterwards. (default: `False`) |
| `--remote` | `str` | Remote to push to (with --push). (default: `'origin'`) |
| `--verify` | `bool` | Run Validate against the restored tree before finalizing -- requires --file/--flake. Off by default: an incident rollback should be fast, and the restored tree was already validated when it was first deployed. (default: `False`) |

#### `pynix ekn validate`

Boot real etcd+kube-apiserver, apply manifests, and run kubeconform.

| Argument | Type | Help |
| --- | --- | --- |
| `--file` | `Path or None` |  (default: `None`) |
| `--flake` | `str or None` |  (default: `None`) |
| `--attr` | `str or None` | Dot-separated attribute path within the evaluation result. (default: `None`) |

#### `pynix ekn kubeapply`

Apply Kubernetes objects directly against the current kubeconfig
context: server-side apply in barrier order, with optional pruning.

General-purpose primitive backing both one-time direct bootstraps
(`--target bootstrap` -- gets a GitOps engine running before it can
sync itself) and `ekn validate`'s ephemeral-apiserver conformance runs
(which apply the full `kubernetes.generated` set). Decrypts any object
carrying a `sops:` metadata block (see ekn.sops) before applying it --
SOPS-encrypted objects flow untouched through `ekn commit`'s GitOps
path, but a direct apply has no ArgoCD+kustomize+ksops step to do that
decryption for it. Also ensures every `kubernetes.sopsAgeIdentities`
entry exists as a Secret first (generating a fresh age keypair the
first time one is missing) -- any easykubenix consumer that needs a
SOPS-decrypting workload bootstrapped (e.g. argocd.nix's ksops
support) declares it there instead of a bespoke bootstrap script.

| Argument | Type | Help |
| --- | --- | --- |
| `--file` | `Path or None` |  (default: `None`) |
| `--flake` | `str or None` |  (default: `None`) |
| `--attr` | `str or None` | Dot-separated attribute path within the evaluation result. (default: `None`) |
| `--target` | `str or None` | Apply only this GitOps target's objects (kubernetes.gitOpsTargets). Omit for the full kubernetes.generated set. (default: `None`) |
| `--prune` | `bool` | Delete previously-applied (same discriminator) objects no longer present in this apply. (default: `False`) |
| `--confirm-context` | `str or None` | Prompt for confirmation unless the current kubectl context ends with this name. (default: `None`) |

#### `pynix ekn clusterdiff`

Diff Kubernetes objects against the live cluster.

Unlike `ekn diff` (which compares against the previous GitOps commit),
this compares against the cluster's actual current state -- for each
object, a server-side-apply dry run shows what `ekn kubeapply`/
`ekn validate` would really change right now, including drift from
manual kubectl edits or other controllers. Read-only: nothing is
applied, pruned, or waited on.

| Argument | Type | Help |
| --- | --- | --- |
| `--file` | `Path or None` |  (default: `None`) |
| `--flake` | `str or None` |  (default: `None`) |
| `--attr` | `str or None` | Dot-separated attribute path within the evaluation result. (default: `None`) |
| `--target` | `str or None` | Diff only this GitOps target's objects (kubernetes.gitOpsTargets). Omit for the full kubernetes.generated set. (default: `None`) |

#### `pynix ekn pushcache`

Build a Nix attribute and copy its realised closure to a remote store.

Manual/ad-hoc escape hatch (or for CI) for pushing an arbitrary
attribute's closure -- for the routine per-deploy case, `ekn deploy`
already does this automatically from `ekn.cacheTo`/`ekn.cachePackage`,
no flags needed (see `Deploy`).

| Argument | Type | Help |
| --- | --- | --- |
| `--file` | `Path or None` |  (default: `None`) |
| `--flake` | `str or None` |  (default: `None`) |
| `--attr` | `str` | Dot-separated attribute path to build and push, e.g. 'kubenix.config.kluctl.projectDir'. *(required)* |
| `--to` | `str` | Destination store URI, e.g. ssh-ng://nix@host:2222 *(required)* |
| `--substitute-on-destination` | `bool` | Let the destination substitute from its own configured caches instead of streaming everything. (default: `True`) |
| `--check-sigs` | `bool` | Verify signatures when copying (off by default, matching kluctl's existing preDeployScript). (default: `False`) |

#### `pynix ekn split-manifest`

Split a JSON manifest list into a namespace/kind/name.yaml directory tree.

Internal: used by easykubenix's `manifestYAMLDir` derivation so the whole
GitOps tree renders as a single build instead of one derivation per
object. Not intended for interactive use.

| Argument | Type | Help |
| --- | --- | --- |
| `json-file` | `Path` |  *(required)* |
| `out-dir` | `Path` |  *(required)* |

#### `pynix ekn _yamlToJson`

Parse a YAML document stream on stdin and dump it as a JSON array on stdout.

Internal: the IFD-derivation fallback importyaml.nix shells out to when
nanopynix's fromYAML11Stream/fromYAMLStream primops aren't registered
(plain `nix build`/`nix eval`, no ekn worker attached). Reuses the exact
same nanopynix.primops YAML-parsing code the in-process primop path
uses, so both paths agree on YAML 1.1 vs 1.2 scalar semantics (e.g. a
volume's `defaultMode: 0644` as octal) -- unlike the `yq`-based approach
this replaced. Not intended for interactive use.

| Argument | Type | Help |
| --- | --- | --- |
| `--yaml-version` | `Literal[yaml11, yaml12]` | YAML version to parse the input stream with. (default: `'yaml12'`) |

#### `pynix ekn _jsonToYAML`

Parse a JSON value on stdin and dump it as YAML on stdout.

Internal: the reverse of `_yamlToJson`, reusing nanopynix's `to_yaml`
(root lists render as a `---`-separated document stream) so a
derivation-fallback path stays byte-for-byte consistent with the
in-process `toYAML` primop. Not intended for interactive use.

