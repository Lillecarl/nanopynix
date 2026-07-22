# pynix-lsp: easykubenixEntry = import ../default.nix { }
#
# A real easykubenix author might wrap a module's definitions in an
# explicit `config = { ... };` (the NixOS-module convention -- see
# ../../module_system/config1.nix) instead of the flat/implicit style
# demo.nix uses. Regression fixture proving `_module.args` resolution
# (see demo.nix's LSPOINT1) is indifferent to that wrapper too:
# `identifier_path_at` only ever looks at the innermost enclosing
# binding's own attrpath, never an outer `config` key. (Every binding in
# this file must live inside this one `config = { ... };` -- the module
# system rejects a file that has an explicit `config` attribute AND other
# top-level attributes alongside it.)
#
# Below that, this file also exercises every dot boundary along a path
# shaped like `kubernetes.objects.<namespace>.<kind>.<name>.<field>...`:
# top-level key, namespace, Kind, instance name, and three levels of
# OpenAPI-schema-backed field completion (Deployment -> DeploymentSpec ->
# PodTemplateSpec -> PodSpec, i.e. a real multi-hop `$ref` chain). Each
# marker below sits immediately after one such dot -- an empty-partial
# cursor position, exactly where a client's completion popup fires the
# moment `.` is typed (the server declares `.` as its sole trigger
# character; see _handlers.py). `kubernetes.objects.none.Namespace.default`
# is declared purely so namespace completion (sourced from that bucket's
# own keys -- see `EasykubenixDialect._namespace_names`) has something
# real to suggest.
#
# Marker comments below are for tests/pynix/test_lsp_scenarios.py
# (see tests/support/lsp_markers.py) -- not part of the easykubenix
# config itself.
{ lib, pkgs, ... }:
{
  config = {
#                                                                              LSPOINT1v"s"
    kubernetes.objects.default.ConfigMap.demo.data.platform = pkgs.stdenv.hostPlatform.system;

    kubernetes.objects.none.Namespace.default = { };

#              vLSPOINTTOP"o"
    kubernetes.objects.default.Deployment.p1 = { };

#                      vLSPOINTNS"d"
    kubernetes.objects.default.Deployment.p2 = { };

#                              vLSPOINTKIND"D"
    kubernetes.objects.default.Deployment.p3 = { };

#                                         vLSPOINTNAME"p"
    kubernetes.objects.default.Deployment.p4 = { };

#                                            vLSPOINTF1"s"
    kubernetes.objects.default.Deployment.p5.spec = { };

#                                                 vLSPOINTF2"t"
    kubernetes.objects.default.Deployment.p6.spec.template = { };

#                                                          vLSPOINTF3"s"
    kubernetes.objects.default.Deployment.p7.spec.template.spec = { };

#                                                               vLSPOINTF4"c"
    kubernetes.objects.default.Deployment.p8.spec.template.spec.containers = [ ];
  };
}
