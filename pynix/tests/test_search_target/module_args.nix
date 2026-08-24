# A module system that hands `pkgs` to its modules through `_module.args`, and
# re-exports nothing at the top. `lib.evalModules` removes `_module` from
# `config` and puts it beside `config`, so the package set is at
# `_module.args.pkgs` and the second link of `PKGS_CHAIN` answers.
{ }:
let
  default = import ../../../. { };
  inherit (default) pkgs lib;
in
lib.evalModules {
  modules = [
    { _module.args.pkgs = pkgs; }
    ../test_search/module.nix
  ];
}
