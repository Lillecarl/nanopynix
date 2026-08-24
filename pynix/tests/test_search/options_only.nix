# A module system that can be indexed for options and holds no package set.
#
# `test_search_target/special_args.nix` hides `pkgs` and re-exports nothing, so
# it has no `lib` either and neither half of the search can run. This one
# re-exports `lib` alone, which is the shape that answers for options and not
# for packages.
{ }:
let
  default = import ../../../. { };
  inherit (default) lib;
  evaluated = lib.evalModules {
    specialArgs.pkgs = default.pkgs;
    modules = [ ./module.nix ];
  };
in
evaluated // { inherit lib; }
