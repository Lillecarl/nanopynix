#!/usr/bin/env bash
# Unpack one wheel into the site-packages layout of the environment.
#
# `graph.nix` runs this as the builder of one node of the graph. It is a file,
# and not a string inside `graph.nix`, so that no `$` and no `${` needs an
# escape, and so that `shellcheck` reads it.
#
# The environment gives:
#   wheel  the `.whl` file, which the fetch of the graph produced
#   site   the site-packages directory, relative to the output
#   out    the output path
# SC2154: graph.nix passes each variable below as an attribute of the
# derivation, so the build environment holds it.
# shellcheck disable=SC2154
set -eu

mkdir -p "$out/$site"
unzip -q -o "$wheel" -d "$out/$site"
