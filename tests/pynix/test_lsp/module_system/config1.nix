# pynix-lsp: moduleEntry = import ./default.nix { extraModules = [ ./config1.nix ]; }
#
# A stand-in for a real NixOS configuration.nix, explicit-wrapper style: the
# module system supports both `{ config, ... }: { config = { ... }; }` (this
# file) and the flat/implicit form with no `config` key at all -- see
# config2.nix for that one. `moduleEntry` points at the raw `evalModules`
# result; `config`/`options` roots are derived from it automatically, so
# typing `config.` anywhere resolves against the merged config, and a bare
# attribute path with no prefix (the shape a real definition takes, e.g.
# `services.` below) resolves against the declared options tree instead.
# Any other top-level lambda argument this file itself declares (`pkgs`
# below) is also inferred from `moduleEntry` -- tried against
# `_module.specialArgs` first, then a module-contributed `_module.args`,
# matching nixpkgs' own argument-resolution precedence -- so `pkgs.hello`-
# style completion is real package completion (this exact nixpkgs instance,
# overlays and all), not a guess. A file that never took a `pkgs` argument
# wouldn't get this at all; see `top_level_lambda_formals` in `_syntax.py`.
{ config, pkgs, lib, ... }:
{
  config = {
    programs.example.enable = true;
    programs.example.package = pkgs.hello;
    programs.example.settings.greeting = "hello from config1.nix";

    services.example-daemon.enable = true;
    services.example-daemon.port = 9090;
    services.example-daemon.extraFlags = [ "--verbose" ];
  };
}
