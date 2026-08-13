# pynix-lsp: moduleEntry = import ./default.nix { extraModules = [ ./config2.nix ]; }
#
# The other NixOS configuration.nix shape -- implicit config, no `config = {
# ... };` wrapper at all. This is the shape most real configuration.nix
# files actually use: since this module's returned attrset has no
# options/config/imports key of its own, the module system treats the whole
# thing as `config` implicitly. Try typing `services.` here too, same as
# config1.nix -- and `pkgs.` too, see config1.nix's comment for how
# `moduleEntry` infers it (and every other declared lambda argument).
{ config, pkgs, lib, ... }:
{
  programs.example.enable = true;
  programs.example.package = pkgs.hello;
  programs.example.settings.greeting = "hello from config2.nix";

  services.example-daemon.enable = true;
  services.example-daemon.port = 9191;
  services.example-daemon.extraFlags = [ "--quiet" ];
}
