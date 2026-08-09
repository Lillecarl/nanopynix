#!/usr/bin/env bash
# Run the nanopynix `builder-rpc-v0` example against a Nix from master.
#
# The system daemon of this machine is too old for the feature, and the store
# it owns is not writable by this user. This script therefore builds in a
# private chroot store, where the client does the building itself and the new
# code path runs. The store directory stays `/nix/store`, so every dependency
# substitutes from the binary cache instead of building.
#
# Two things differ from `ddrn/examples/submitted/run.sh`. This example needs a
# nanopynix built against the same Nix that builds it, so the script builds
# `nanopynixMaster` from this repository. And the example submits a `.drv`
# rather than a finished output, so the script then realises that `.drv` with
# `builtins.outputOf` and prints what the graph produced.
#
# Usage: ddrn/examples/submitted-graph/run.sh [work-directory]
set -euo pipefail

# A Nix revision that has the feature. `builder-rpc-v0` merged on 2026-07-21
# in NixOS/nix#15793, so any master revision after that date works. This must
# agree with `nix/nix-master.nix`, which pins the Nix that nanopynix links.
NIX_REV="${NIX_REV:-adee431334cd12f3a33764ac86284220cef4d204}"

work="${1:-${TMPDIR:-/tmp}/ddrn-builder-rpc-graph}"
mkdir -p "$work"
store="$work/store"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"

features='nix-command flakes ca-derivations dynamic-derivations'

echo "==> building Nix $NIX_REV"
nix build "github:NixOS/nix/$NIX_REV#nix" \
  --extra-experimental-features 'nix-command flakes' \
  --out-link "$work/nix-master" --print-build-logs

nix_master="$(readlink -f "$work/nix-master")"
echo "==> $("$nix_master/bin/nix" --version)"

echo "==> building nanopynix against that Nix"
# `mkVirtualEnv` takes a name and a spec, so this is a function call and not an
# attribute path. `--expr` is the only form that can express it.
# `FLAKE_COMPATISH_DISABLE_OVERRIDES` makes this evaluation agree with a flake
# evaluation, the same way every CI workflow sets it.
FLAKE_COMPATISH_DISABLE_OVERRIDES=1 nix build --impure \
  --expr "(import $repo { }).nanopynixMaster.pythonSet.mkVirtualEnv \"ddrn-planner-env\" { nanopynix = [ ]; }" \
  --out-link "$work/nanopynix-env" --print-build-logs

nanopynix_env="$(readlink -f "$work/nanopynix-env")"
echo "==> nanopynix env at $nanopynix_env"

echo "==> seeding the private store at $store"
mkdir -p "$store"
# `--no-check-sigs` is needed and is safe here. The nanopynix environment was
# built on this machine, so no binary cache signed it, and a chroot store
# refuses an unsigned path by default: "cannot add path '...' because it lacks
# a signature by a trusted key". The store is a private directory that this
# script created, and the source is the local store of this same machine.
"$nix_master/bin/nix" copy --to "$store" --no-check-sigs "$nix_master" "$nanopynix_env" \
  --extra-experimental-features 'nix-command flakes'

echo "==> registering the graph"
# The output of this build is the root `.drv` of the graph, and not a finished
# store object: a `builder-rpc-v0` builder cannot realise anything.
"$nix_master/bin/nix" build \
  --store "$store" \
  --file "$here" \
  --argstr nanopynixEnv "$nanopynix_env" \
  --impure \
  --no-link --print-out-paths --print-build-logs \
  --extra-experimental-features "$features" \
  --system-features 'builder-rpc-v0 recursive-nix'

echo "==> realising the graph"
# `builtins.outputOf` takes the output of the planner, which is a `.drv`, and
# names an output of *that* derivation. This is the ordinary
# dynamic-derivation pattern; what `builder-rpc-v0` changed is only how the
# planner produced its `.drv`.
result="$("$nix_master/bin/nix" build \
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
"$nix_master/bin/nix" --store "$store" \
  --extra-experimental-features "$features" \
  store cat "$result/a"
"$nix_master/bin/nix" --store "$store" \
  --extra-experimental-features "$features" \
  store cat "$result/b"
