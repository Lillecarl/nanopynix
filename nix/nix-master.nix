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
  fetchFromGitHub,
  nixVersions,
}:

{
  # A revision of the default branch of Nix, later than 2026-07-21.
  rev ? "9137203c1d85c9d13b3d1ef91ba8885b185e5947",
  hash ? "sha256-CFcOMFIJGsvNsDt5awut3tf8woW/4XD3VPs2u8htpV0=",
  # The version that this revision reports.
  #
  # **Both overrides below are necessary, and the second one is easy to
  # forget.** `overrideSource` replaces the source and leaves the version
  # alone, so the scope would keep saying `2.35pre20260619_f8bb823a` while
  # building a 2.36 source. `nanopynix-bindings` derives
  # `NANOPYNIX_NIX_VERSION_NUMBER` from that string, so a stale version does
  # not fail: it silently compiles the pre-2.36 branch of every `#if`, and the
  # feature this file exists for disappears with no error.
  version ? "2.36.0pre20260806_9137203",
}:

let
  src = fetchFromGitHub {
    owner = "NixOS";
    repo = "nix";
    inherit rev hash;
  };
in
(nixVersions.nixComponents_git.overrideSource src).overrideScope (
  _final: _prev: { inherit version; }
)
