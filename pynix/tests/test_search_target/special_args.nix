# A module system that hands `pkgs` to its modules through `specialArgs` and
# re-exports nothing. `specialArgs` reaches a module and never reaches the
# result, so no chain finds the package set here. This is the shape that makes
# `--pkgs` necessary rather than convenient.
{ }:
let
  default = import ../../../. { };
  inherit (default) pkgs lib;
in
lib.evalModules {
  specialArgs.pkgs = pkgs;
  modules = [ ../test_osearch/module.nix ];
}
