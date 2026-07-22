# pynix-lsp: moduleEntry = (import ../default.nix { }).moduleSystem
#
# Adapted from easykubenix's own ~/Code/easykubenix/demo/resources.nix --
# a real example already in the upstream repo, trimmed down to exercise
# pynix-lsp's existing (not easykubenix-specific) ModuleSystemDialect
# machinery rather than any new dialect code, with zero new Python code:
#
# - LSPOINT1 hovers `pkgs.stdenv.hostPlatform.system` -- `pkgs` resolves
#   via `_module.args` exactly like a NixOS module's `pkgs` would (see
#   ../default.nix).
# - LSPOINT2 hovers the `objects` segment of a flat top-level binding --
#   deliberately NOT a per-instance path (`kubernetes.objects.<ns>.<Kind>.
#   <name>.<field>`): nixpkgs' module system does not expose per-instance
#   `options.<path>` recursion for `attrsOf submodule`-typed options at
#   all (confirmed with an isolated evalModules repro, independent of
#   easykubenix's own design) -- so this targets `options.kubernetes.
#   objects`'s own real, top-level mkOption description instead, the
#   deepest path this mechanism can actually resolve.
#
# LSPOINT comments below are scenario markers for
# tests/pynix/test_lsp_scenarios.py (see tests/support/lsp_markers.py) --
# not part of the easykubenix config itself.
{ lib, pkgs, ... }:
{
  kubernetes.objects.default.Secret.appConfig = {
    metadata.labels.app = "my-app";
    stringData."config.yaml" = "some-value";
#                                          LSPOINT1v"s"
    stringData.platform = pkgs.stdenv.hostPlatform.system;
  };

#    LSPOINT2v"o"
  kubernetes.objects.default.Deployment.demo.spec.replicas = 1;
}
