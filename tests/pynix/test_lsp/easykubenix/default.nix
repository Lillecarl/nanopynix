# An easykubenix (https://github.com/Lillecarl/easykubenix) example: a
# NixOS-module-style system for generating Kubernetes manifests.
#
# Goes through easykubenix's own real `default.nix` entry point (not a
# hand-rolled `lib.evalModules` call) -- unlike terranix, easykubenix's
# `default.nix` already exposes the *raw* `lib.evalModules` result via
# `passthru.eval`: real `config`/`options`/`_module`, no un-hiding
# workaround needed (see `../terranix/default.nix`'s `moduleSystem` comment
# for what terranix has to work around instead). That makes `moduleSystem`
# below directly usable as a `# pynix-lsp: moduleEntry = ...` target, the
# same plain `ModuleSystemDialect` contract any NixOS module fixture uses.
# No easykubenix-specific Python dialect code exists yet (see the project
# memory / plan for the deferred OpenAPI-schema-backed follow-up).
{ }:
let
  default = import ../../../../. { };
  inherit (default) pkgs;

  flake = import ../../../../nix/compat.nix;
  easykubenixSrc = flake.inputs.easykubenix;

  testModules = [
    ./modules/demo.nix
    ./modules/config.nix
  ];

  eku = import easykubenixSrc {
    inherit pkgs;
    modules = testModules;
  };
in
{
  inherit pkgs;
  moduleSystem = eku.passthru.eval;
}
