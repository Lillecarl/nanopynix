# pynix-lsp: easykubenixEntry = import ../default.nix { }
#
# Adapted from easykubenix's own ~/Code/easykubenix/demo/resources.nix --
# a real example already in the upstream repo, trimmed down to exercise both
# pynix-lsp's existing ModuleSystemDialect machinery and the newer
# EasykubenixDialect (see pynix/src/pynix/_lsp/_easykubenix.py):
#
# - The first hover marker below targets `pkgs.stdenv.hostPlatform.system` --
#   `pkgs` resolves via `_module.args` exactly like a NixOS module's `pkgs`
#   would (see ../default.nix). Still handled by ModuleSystemDialect, not the
#   newer dialect.
# - The second hover marker targets the `objects` segment of a flat
#   top-level binding -- deliberately NOT a per-instance path
#   (`kubernetes.objects.<ns>.<Kind>.<name>.<field>`): nixpkgs' module system
#   does not expose per-instance `options.<path>` recursion for `attrsOf
#   submodule`-typed options at all (confirmed with an isolated evalModules
#   repro, independent of easykubenix's own design) -- so this targets
#   `options.kubernetes.objects`'s own real, top-level mkOption description
#   instead, the deepest path this mechanism can actually resolve. Also
#   handled by ModuleSystemDialect.
# - The third hover marker targets the bare Kind name itself -- resolved by
#   the newer dialect via the Kubernetes OpenAPI schema, rendering that
#   Kind's own top-level description.
# - The fourth hover marker targets a real field nested inside the object
#   body (`spec.replicas`) -- also resolved by the newer dialect, by walking
#   the OpenAPI schema definition for this Kind.
# - The range marker on the second Deployment's `replicas` field is retyped
#   from scratch by the completion scenario, proving `EasykubenixDialect`'s
#   `complete` (not just `hover`) also walks the OpenAPI schema.
# - The sixth hover marker and seventh range marker use *nested* attrset
#   style (`Kind.name = { spec = { ... }; };`, rather than one fully flat
#   dotted binding) -- the natural NixOS-module way to write a Kubernetes
#   object body. `identifier_path_at`/`completion_target_at` alone only ever
#   see the innermost binding's own attrpath, losing the outer
#   `kubernetes.objects.<ns>.<kind>.<name>` prefix entirely; both dialect
#   methods recover it via `enclosing_binding_path_at` (see
#   pynix/src/pynix/_lsp/_syntax.py), climbing every wrapping
#   attrset-valued binding.
#
# Marker comments below are scenario markers for
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

#    LSPOINT2v"o"    LSPOINT3v"D"         LSPOINT4v"r"
  kubernetes.objects.default.Deployment.demo.spec.replicas = 1;

#                                          LSSTART5v"r"    vLSEND5
  kubernetes.objects.default.Deployment.demo2.spec.replicas = 1;

  kubernetes.objects.default.Deployment.demoNested = {
    spec = {
#     vLSPOINT6"r"
      replicas = 2;
    };
  };

  kubernetes.objects.default.Deployment.demoNested2 = {
#                       LSSTART7v"m"    vLSEND7
                                metadata = { };
  };
}
