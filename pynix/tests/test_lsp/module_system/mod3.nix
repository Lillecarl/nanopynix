{ config, pkgs, lib, ... }:
{
  options.services.example-daemon = {
    enable = lib.mkEnableOption "the example daemon";
    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.hello;
      description = "Package providing the example daemon.";
    };
    port = lib.mkOption {
      type = lib.types.port;
      default = 8080;
      description = "Port the example daemon listens on.";
    };
    extraFlags = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "Extra command-line flags passed to the daemon.";
      example = [ "--verbose" ];
    };
  };
}
