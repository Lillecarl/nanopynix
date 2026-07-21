{ config, lib, pkgs, ... }:
{
  options.services.foo.enable = lib.mkEnableOption "foo";
  config = lib.mkIf config.services.foo.enable {
    systemd.services.foo = { };
  };
}
