#!/usr/bin/env bash
# Resolve a Python environment as a graph, and then run it.
#
# The system daemon of this machine is too old for the feature, and the store
# it owns is not writable by this user. This script therefore builds in a
# private chroot store, where the client does the building itself and the new
# code path runs. The store directory stays `/nix/store`, so every dependency
# substitutes from the binary cache instead of building.
#
# **This example needs the patched Nix, and takes it from one source.** Both
# the `nix` binary and the nanopynix environment come from `nanopynixMaster`,
# which reads the working tree that `nix/nix-master.nix` names, so the client
# and the store agree by construction.
#
# The graph downloads from PyPI, so this script needs a network.
#
# Usage: ddrn/examples/venv-graph/run.sh [work-directory]
set -euo pipefail

work="${1:-${TMPDIR:-/tmp}/ddrn-venv-graph}"
mkdir -p "$work"
store="$work/store"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"

features='nix-command flakes ca-derivations dynamic-derivations'

echo "==> building the patched Nix"
# `FLAKE_COMPATISH_DISABLE_OVERRIDES` makes this evaluation agree with a flake
# evaluation, the same way every CI workflow sets it.
FLAKE_COMPATISH_DISABLE_OVERRIDES=1 nix build --impure \
  --expr "(import $repo { }).nanopynixMaster.nix-cli" \
  --out-link "$work/nix-patched" --print-build-logs

nix_patched="$(readlink -f "$work/nix-patched")"
echo "==> $("$nix_patched/bin/nix" --version)"

echo "==> building nanopynix against that same Nix"
FLAKE_COMPATISH_DISABLE_OVERRIDES=1 nix build --impure \
  --expr "(import $repo { }).nanopynixMaster.pythonSet.mkVirtualEnv \"ddrn-planner-env\" { nanopynix = [ ]; }" \
  --out-link "$work/nanopynix-env" --print-build-logs

nanopynix_env="$(readlink -f "$work/nanopynix-env")"
echo "==> nanopynix env at $nanopynix_env"

echo "==> seeding the private store at $store"
mkdir -p "$store"
# `--no-check-sigs` is needed and is safe here. Both inputs were built on this
# machine, so no binary cache signed them, and a chroot store refuses an
# unsigned path by default. The store is a private directory that this script
# created, and the source is the local store of this same machine.
"$nix_patched/bin/nix" copy --to "$store" --no-check-sigs "$nix_patched" "$nanopynix_env" \
  --extra-experimental-features 'nix-command flakes'

echo "==> resolving the environment"
# The output of this build is the root `.drv` of the graph. Nothing is
# downloaded yet: the planner names the artefacts it chose, and the fetches are
# nodes of the graph that it registered.
drv="$("$nix_patched/bin/nix" build \
  --store "$store" \
  --file "$here" \
  --argstr nanopynixEnv "$nanopynix_env" \
  --impure \
  --no-link --print-out-paths --print-build-logs \
  --extra-experimental-features "$features" \
  --system-features 'builder-rpc-v0 recursive-nix')"

echo "==> the planner submitted $drv"
case "$drv" in
*-demo-venv.drv) ;;
*)
  echo "expected an output named 'demo-venv.drv', got '$drv'" >&2
  exit 1
  ;;
esac

# The graph is a graph, and not one derivation. Count the nodes under the root.
echo "==> the root of the graph reads:"
"$nix_patched/bin/nix" --store "$store" \
  --extra-experimental-features "$features" \
  derivation show "$drv" | grep -o '"[^"]*\.drv"' | sort -u

echo "==> building the environment"
result="$("$nix_patched/bin/nix" build \
  --store "$store" \
  --impure \
  --no-link --print-out-paths --print-build-logs \
  --extra-experimental-features "$features" \
  --expr "
    let
      planner = import $here {
        pkgs = import <nixpkgs> { };
        nanopynixEnv = \"$nanopynix_env\";
      };
    in
    builtins.outputOf planner.outPath \"out\"
  ")"

echo "==> the environment is $result"
"$nix_patched/bin/nix" --store "$store" \
  --extra-experimental-features "$features" \
  store cat "$result/manifest"

# It is a real virtual environment, and not a directory with a wrapper.
echo "==> it is a virtual environment:"
"$nix_patched/bin/nix" --store "$store" \
  --extra-experimental-features "$features" \
  store cat "$result/pyvenv.cfg"

# **The proof.** `idna` was in the lock file as a source distribution only, so
# the environment holds it only if the graph built it with the backend that the
# planner resolved from the same lock file. `bin/idna` exists only if the
# installer that the planner also resolved wrote the console script, because an
# unpacked wheel carries no such file.
#
# The check runs as a derivation, and not from this shell. `make-venv.py`
# writes an absolute symlink for each entry, which is what every store path
# does, and a chroot store puts the store under a prefix. Those symlinks
# resolve inside a build of that store, and not from outside it.
echo "==> running it"
check="$("$nix_patched/bin/nix" build \
  --store "$store" \
  --file "$here/check.nix" \
  --argstr nanopynixEnv "$nanopynix_env" \
  --impure \
  --no-link --print-out-paths --print-build-logs \
  --extra-experimental-features "$features")"

"$nix_patched/bin/nix" --store "$store" \
  --extra-experimental-features "$features" \
  store cat "$check"

# **The editable proof.** The environment holds `myapp` as a PEP 660 editable,
# and the `.pth` that the graph installed expands `$DDRN_EDITABLE_ROOT` when
# the interpreter starts. So a second tree reads back differently from the
# **same** environment, and nothing about the environment is rebuilt.
echo "==> the same environment, a different tree"
other="$work/other-tree"
rm -rf "$other"
cp -R "$here/fixtures/myapp" "$other"
chmod -R u+w "$other"
sed -i 's|^WHICH_TREE = .*|WHICH_TREE = "a tree that this run wrote a minute ago"|' \
  "$other/src/myapp/__init__.py"

check_other="$("$nix_patched/bin/nix" build \
  --store "$store" \
  --file "$here/check.nix" \
  --argstr nanopynixEnv "$nanopynix_env" \
  --arg editableTree "$other" \
  --impure \
  --no-link --print-out-paths --print-build-logs \
  --extra-experimental-features "$features")"

"$nix_patched/bin/nix" --store "$store" \
  --extra-experimental-features "$features" \
  store cat "$check_other"

# The environment is one store path, and both checks read it. Prove that,
# rather than say it: each check derivation names its input.
echo "==> both checks read the same environment:"
for path in "$check" "$check_other"; do
  "$nix_patched/bin/nix" --store "$store" \
    --extra-experimental-features "$features" \
    path-info --json --json-format 1 "$path" |
    grep -o '[a-z0-9]\{32\}-demo-venv' | sort -u
done
