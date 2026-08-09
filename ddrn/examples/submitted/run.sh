#!/usr/bin/env bash
# Run the `builder-rpc-v0` example against a Nix from master.
#
# The system daemon of this machine is too old for the feature, and the store
# it owns is not writable by this user. This script therefore builds in a
# private chroot store, where the client does the building itself and the new
# code path runs. The store directory stays `/nix/store`, so every dependency
# substitutes from the binary cache instead of building.
#
# Usage: ddrn/examples/submitted/run.sh [work-directory]
set -euo pipefail

# A Nix revision that has the feature. `builder-rpc-v0` merged on 2026-07-21
# in NixOS/nix#15793, so any master revision after that date works.
#
# **A revision of today compiles, and an older one substitutes.** Hydra builds
# master, but it lags the branch by some hours. Set `NIX_REV` to a revision
# that `cache.nixos.org` already holds to skip a build of Nix that takes about
# 20 minutes.
NIX_REV="${NIX_REV:-adee431334cd12f3a33764ac86284220cef4d204}"

work="${1:-${TMPDIR:-/tmp}/ddrn-builder-rpc}"
mkdir -p "$work"
store="$work/store"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> building Nix $NIX_REV"
nix build "github:NixOS/nix/$NIX_REV#nix" \
  --extra-experimental-features 'nix-command flakes' \
  --out-link "$work/nix-master" --print-build-logs

nix_master="$(readlink -f "$work/nix-master")"
echo "==> $("$nix_master/bin/nix" --version)"

echo "==> seeding the private store at $store"
mkdir -p "$store"
# `--no-check-sigs` is needed and is safe here. A chroot store refuses an
# unsigned path by default, and this machine builds the Nix itself when
# `cache.nixos.org` has no build of `NIX_REV` yet. The store is a private
# directory that this script created, and the source is the local store of
# this same machine.
"$nix_master/bin/nix" copy --to "$store" --no-check-sigs "$nix_master" \
  --extra-experimental-features 'nix-command flakes'

echo "==> building the example"
"$nix_master/bin/nix" build \
  --store "$store" \
  --file "$here" \
  --argstr nixMaster "$nix_master" \
  --impure \
  --no-link --print-out-paths --print-build-logs \
  --extra-experimental-features 'nix-command flakes ca-derivations dynamic-derivations' \
  --system-features 'builder-rpc-v0 recursive-nix'
