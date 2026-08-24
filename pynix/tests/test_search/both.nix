# A module system that also carries a small package set, so that the default
# `pynix search` indexes both halves in one run.
#
# The package set is the fixture of `pynix/tests/test_packages/`, and not real
# nixpkgs: a walk of the real one costs 15 s and 2 GB, which no test can pay.
# It carries `path` and `stdenv`, so `pynix._search_target` reads it as a
# package set wherever it is found.
{ }:
let
  default = import ../../../. { };
  inherit (default) lib;
  small = import ../test_packages/pkgset.nix { };
  evaluated = lib.evalModules {
    specialArgs.pkgs = default.pkgs;
    modules = [ ./module.nix ];
  };
in
evaluated
// {
  pkgs = small;
  inherit lib;
}
