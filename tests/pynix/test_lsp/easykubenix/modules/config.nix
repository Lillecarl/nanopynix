# pynix-lsp: easykubenixEntry = import ../default.nix { }
#
# A real easykubenix author might wrap a module's definitions in an
# explicit `config = { ... };` (the NixOS-module convention -- see
# ../../module_system/config1.nix) instead of the flat/implicit style
# demo.nix uses. Regression fixture proving `_module.args` resolution
# (see demo.nix's LSPOINT1) is indifferent to that wrapper too:
# `identifier_path_at` only ever looks at the innermost enclosing
# binding's own attrpath, never an outer `config` key.
#
# The marker comment below is for tests/pynix/test_lsp_scenarios.py
# (see tests/support/lsp_markers.py) -- not part of the easykubenix
# config itself.
{ lib, pkgs, ... }:
{
  config = {
#                                                                              LSPOINT1v"s"
    kubernetes.objects.default.ConfigMap.demo.data.platform = pkgs.stdenv.hostPlatform.system;
  };
}
