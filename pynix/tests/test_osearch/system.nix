# A minimal nixosSystem-shaped value: `options`/`config` from `lib.evalModules`
# plus a top-level `pkgs` sibling, exactly like the real `nixosSystem` wrapper
# exposes -- this is the shape `pynix osearch` expects by default (`options`
# and `pkgs.lib`).
{ }:
let
  default = import ../../../. { };
  inherit (default) pkgs lib;
  evaluated = lib.evalModules {
    specialArgs.pkgs = pkgs;
    modules = [ ./module.nix ];
  };
in
evaluated // { inherit pkgs; }
