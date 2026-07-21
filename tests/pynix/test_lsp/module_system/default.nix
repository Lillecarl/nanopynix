{ extraModules ? [ ] }:
let
  default = import ../../../../. { };
  inherit (default) pkgs lib;
in
lib.evalModules {
  specialArgs.pkgs = pkgs;
  modules = [
    ./mod1.nix
    ./mod2.nix
    ./mod3.nix
  ] ++ extraModules;
}
