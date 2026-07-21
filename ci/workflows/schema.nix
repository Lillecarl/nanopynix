# NixOS-module-system schema for GitHub Actions workflows. Jobs are naturally
# a YAML mapping (keyed by job id, order irrelevant -- execution order comes
# from `needs:`, not declaration order) so `jobs` is `attrsOf submodule`.
# Steps are naturally an ordered YAML sequence, so `steps` is `listOf
# submodule` -- a plain Nix list is already ordered, so there's no need for
# an artificial "order" field the way an attrsOf-keyed collection would need
# one.
#
# Every GHA key used here (`runs-on`, `"if"`, `"with"`) is used verbatim as
# the option name: hyphenated and keyword-clashing attribute names both work
# fine as Nix attribute names (quoted for keywords), so no separate
# Nix-name/YAML-name translation layer is needed.
{ lib }:
let
  inherit (lib) mkOption types;

  stepModule = { ... }: {
    # Action `with:` shapes vary per-action and aren't worth modeling
    # exhaustively; freeform passthrough covers anything not listed below
    # (e.g. rare per-step keys like `shell` or `continue-on-error`).
    freeformType = types.attrsOf types.anything;
    options = {
      name = mkOption { type = types.nullOr types.str; default = null; };
      id = mkOption { type = types.nullOr types.str; default = null; };
      uses = mkOption { type = types.nullOr types.str; default = null; };
      run = mkOption { type = types.nullOr types.lines; default = null; };
      "if" = mkOption { type = types.nullOr types.str; default = null; };
      "with" = mkOption { type = types.nullOr (types.attrsOf types.anything); default = null; };
      env = mkOption { type = types.nullOr (types.attrsOf types.str); default = null; };
    };
  };

  jobModule = { ... }: {
    freeformType = types.attrsOf types.anything;
    options = {
      runs-on = mkOption { type = types.str; default = "ubuntu-24.04"; };
      needs = mkOption {
        type = types.nullOr (types.either types.str (types.listOf types.str));
        default = null;
      };
      "if" = mkOption { type = types.nullOr types.str; default = null; };
      permissions = mkOption { type = types.nullOr (types.attrsOf types.str); default = null; };
      environment = mkOption { type = types.nullOr (types.attrsOf types.anything); default = null; };
      concurrency = mkOption { type = types.nullOr (types.attrsOf types.anything); default = null; };
      strategy = mkOption { type = types.nullOr (types.attrsOf types.anything); default = null; };
      outputs = mkOption { type = types.nullOr (types.attrsOf types.str); default = null; };
      steps = mkOption {
        type = types.listOf (types.submodule stepModule);
        default = [ ];
      };
    };
  };

  # Recursively drops unset (null-default) option values so the rendered
  # YAML only contains keys a job/step actually set, matching the old
  # optionalAttrs-based output. Scoped to `jobs` only -- `on` is rendered
  # verbatim, since e.g. on_schedule.nix's `workflow_dispatch = null;` is a
  # deliberate GHA value (manual trigger, no inputs), not an unset option.
  stripNulls =
    value:
    if builtins.isAttrs value then
      lib.mapAttrs (_: stripNulls) (lib.filterAttrs (_: v: v != null) value)
    else if builtins.isList value then
      map stripNulls value
    else
      value;
in
{
  evalWorkflow =
    { name, on, jobs }:
    let
      evaluated = lib.evalModules {
        modules = [
          {
            options = {
              name = mkOption { type = types.str; };
              on = mkOption { type = types.attrsOf types.anything; };
              jobs = mkOption {
                type = types.attrsOf (types.submodule jobModule);
                default = { };
              };
            };
          }
          { inherit name on jobs; }
        ];
      };
      cfg = evaluated.config;
      emptySteps = lib.filterAttrs (_: job: job.steps == [ ]) cfg.jobs;
    in
    if emptySteps != { } then
      throw "workflow '${cfg.name}': job(s) with no steps: ${toString (lib.attrNames emptySteps)}"
    else
      {
        inherit (cfg) name on;
        jobs = stripNulls cfg.jobs;
      };
}
