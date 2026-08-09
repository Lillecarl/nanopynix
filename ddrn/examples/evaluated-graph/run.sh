#!/usr/bin/env bash
# Run the `evaluated-graph` example against the patched Nix of this lab.
#
# The system daemon of this machine is too old for the feature, and the store
# it owns is not writable by this user. This script therefore builds in a
# private chroot store, where the client does the building itself and the new
# code path runs. The store directory stays `/nix/store`, so every dependency
# substitutes from the binary cache instead of building.
#
# **This example needs the patched Nix, and takes it from one source.**
# `ddrn/examples/submitted-graph/run.sh` fetches a Nix from GitHub, because
# that example runs against the protocol as released. This one needs the two
# changes of `ddrn/UPSTREAM.md`, which live in the working tree that
# `nix/nix-master.nix` reads. Both the `nix` binary and the nanopynix
# environment therefore come from `nanopynixMaster`, so the client and the
# store agree by construction.
#
# Usage: ddrn/examples/evaluated-graph/run.sh [work-directory]
set -euo pipefail

work="${1:-${TMPDIR:-/tmp}/ddrn-evaluated-graph}"
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
# `mkVirtualEnv` takes a name and a spec, so this is a function call and not an
# attribute path. `--expr` is the only form that can express it.
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

echo "==> registering the graph"
# The output of this build is the root `.drv` of the graph. A `builder-rpc-v0`
# builder cannot realise anything, so the graph is the deliverable and not a
# finished store object.
#
# **The name of that output is `graph.drv`, and the derivation is named
# `planner`.** That is the name relaxation of this lab, which
# `ddrn/UPSTREAM.md` calls Goal 2.
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
*-graph.drv) ;;
*)
  echo "expected an output named 'graph.drv', got '$drv'" >&2
  exit 1
  ;;
esac

# The store holds a derivation there, and not a store object that carries the
# bytes of one. `nix derivation show` reads it, and runs no build.
"$nix_patched/bin/nix" --store "$store" \
  --extra-experimental-features "$features" \
  derivation show "$drv"

echo "==> realising the graph"
# `builtins.outputOf` takes the output of the planner, which is a `.drv`, and
# names an output of *that* derivation.
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

echo "==> the graph produced $result"
"$nix_patched/bin/nix" --store "$store" \
  --extra-experimental-features "$features" \
  store cat "$result/a"
"$nix_patched/bin/nix" --store "$store" \
  --extra-experimental-features "$features" \
  store cat "$result/b"
