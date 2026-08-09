# A Nix from the default branch, for the `builder-rpc-v0` experiment.
#
# `pkgs.nixVersions.git` is not new enough. `builder-rpc-v0` merged on
# 2026-07-21 in NixOS/nix#15793, and the revision that nixpkgs pins inside
# `nixVersions.git` predates it. A `nix flake update` does not move that
# revision, so this file names one.
#
# **This is a from-source build of Nix.** The packaging expressions come from
# the nixpkgs pin and the source does not, so nothing here is in a binary
# cache. That is why `default.nix` keeps it off every CI matrix, in the same
# way and for the same reason as `nanopynixZig`.
#
# Read `ddrn/README.md` for what the feature is and what it is for.
{
  lib,
  fetchFromGitHub,
  nixVersions,
}:

let
  # **The default source is a local checkout, because this lab patches Nix.**
  # The goal is to change the daemon and to build nanopynix against the change,
  # so the source has to be a directory that an edit changes. Make one from the
  # Nix checkout at `~/Code/nix`:
  #
  #     jj workspace add --name ddnix -r master@origin ~/Code/ddnix
  #
  # `NANOPYNIX_NIX_MASTER_SRC` names a different directory, and an unset
  # variable reads as the empty string. `builtins.getEnv` also returns the
  # empty string under a pure evaluation, so a flake evaluation needs no
  # `--impure` to reach the line below.
  #
  # The build falls back to `fetchFromGitHub` when the directory is absent, so
  # another machine and a CI runner still evaluate.
  fromEnvironment = builtins.getEnv "NANOPYNIX_NIX_MASTER_SRC";
  candidate = if fromEnvironment != "" then fromEnvironment else "/home/lillecarl/Code/ddnix";
  defaultWorkspace = if builtins.pathExists candidate then candidate else null;
in

{
  # A revision of the default branch of Nix, later than 2026-07-21. The build
  # reads it only when `workspace` is `null`.
  rev ? "adee431334cd12f3a33764ac86284220cef4d204",
  hash ? "sha256-5OGPbefMDQmC4NIJ34EqfTGMHXNvuRU/Y3nZuj5TXsM=",
  # The directory that holds the working tree of Nix, as a string, or `null` to
  # read `rev` from GitHub.
  workspace ? defaultWorkspace,
  # The version that this source reports.
  #
  # **Both overrides below are necessary, and the second one is easy to
  # forget.** `overrideSource` replaces the source and leaves the version
  # alone, so the scope would keep saying `2.35pre20260619_f8bb823a` while
  # building a 2.36 source. `nanopynix-bindings` derives
  # `NANOPYNIX_NIX_VERSION_NUMBER` from that string, so a stale version does
  # not fail: it silently compiles the pre-2.36 branch of every `#if`, and the
  # feature this file exists for disappears with no error.
  #
  # The CMake rule reads `^([0-9]+)\.([0-9]+)` only, so the text after `2.36`
  # is free. A local build says so, and does not claim a revision that it did
  # not read.
  version ? if workspace == null then "2.36.0pre20260809_adee4313" else "2.36.0pre-ddnix",
}:

let
  # **The filter is not a tidiness measure.** `.jj` changes under every `jj`
  # command, and a development shell puts a meson build in `build` and installs
  # it into `outputs`. Each one gives a new store path to every evaluation,
  # which builds the whole of Nix again for a change that no compiler reads.
  #
  # **Only the top of the tree carries those two names.** Nix has eight
  # directories of source called `build` or `outputs`, and
  # `src/libstore/build/build-log.cc` is one file of one of them. A filter on
  # the base name alone drops each of them, and meson then stops with
  # "File build/build-log.cc does not exist". So the test is on the path
  # relative to the root, and not on the base name.
  workspaceSource =
    let
      root = toString (/. + workspace);
    in
    lib.cleanSourceWith {
      name = "nix-source";
      src = /. + workspace;
      filter =
        path: type:
        let
          relative = lib.removePrefix "${root}/" (toString path);
        in
        lib.cleanSourceFilter path type
        && baseNameOf path != ".jj"
        && relative != "build"
        && relative != "outputs";
    };

  src =
    if workspace == null then
      fetchFromGitHub {
        owner = "NixOS";
        repo = "nix";
        inherit rev hash;
      }
    else
      workspaceSource;
in
(nixVersions.nixComponents_git.overrideSource src).overrideScope (
  _final: _prev: { inherit version; }
)
