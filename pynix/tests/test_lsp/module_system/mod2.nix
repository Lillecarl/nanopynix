# pynix-lsp: moduleEntry = import ./default.nix { }
{ config, pkgs, lib, ... }:
{
  config = lib.mkIf config.programs.example.enable {
    services.example-daemon.enable = lib.mkDefault true;
  };
}
