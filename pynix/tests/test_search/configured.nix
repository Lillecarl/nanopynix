# The fixture module system, with values actually set.
#
# `system.nix` declares options and sets none of them, which is what the index
# tests need: every option is at its default. A test of what an option *came
# to* needs the other half, and a `config` block in `module.nix` would change
# what every index test reads.
#
# `vhosts.web` exists here so that a `<name>` placeholder has one real
# instance to bind to. `systemd.services.<name>.name` is the shape a reader
# meets, and `services.example-daemon.vhosts.<name>.port` is the same shape.
{ }:
let
  default = import ../../../. { };
  inherit (default) pkgs lib;
  evaluated = lib.evalModules {
    specialArgs.pkgs = pkgs;
    modules = [
      ./module.nix
      {
        services.example-daemon.port = 9999;
        services.example-daemon.vhosts.web.port = 8081;
      }
    ];
  };
in
evaluated // { inherit pkgs; }
