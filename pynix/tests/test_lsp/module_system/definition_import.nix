# pynix-lsp: moduleEntry = import ./default.nix { extraModules = [ ./definition_import.nix ]; }
{ config, pkgs, lib, ... }:
{
  imports = [
#   vLSPOINTIMPORT"."
    ./mod2.nix
  ];
}
