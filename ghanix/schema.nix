# NixOS-module-system schema for GitHub Actions workflows. Deliberately
# generic -- nothing here is nanopynix-specific, so this file could be
# dropped into any other Nix project rendering its own CI with the same
# toYAML-based approach.
#
# Jobs are naturally a YAML mapping (keyed by job id, order irrelevant --
# execution order comes from `needs:`, not declaration order), so `jobs` is
# `attrsOf submodule`. Steps are naturally an ordered YAML sequence, so
# `steps` is `listOf submodule` -- a plain Nix list is already ordered, so
# there's no need for an artificial "order" field the way an attrsOf-keyed
# collection of steps would need one.
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
      name = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = ''
          Display name for the step in the Actions UI. Steps without one
          show their `run`/`uses` command instead.
        '';
      };
      id = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = ''
          Step id, so later steps or job `outputs` can reference this
          step's outputs as `steps.<id>.outputs.*`.
        '';
      };
      uses = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "Action to run, as `owner/repo@ref`. Mutually exclusive with `run` in practice.";
      };
      run = mkOption {
        type = types.nullOr types.lines;
        default = null;
        description = "Shell command(s) to run. Mutually exclusive with `uses` in practice.";
      };
      "if" = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "GitHub Actions expression gating whether this step runs, e.g. `\${{ !cancelled() }}`.";
      };
      "with" = mkOption {
        type = types.nullOr (types.attrsOf types.anything);
        default = null;
        example = {
          ref = "main";
        };
        description = "Inputs passed to the action named by `uses`. Shape varies per action, so this is intentionally untyped.";
      };
      env = mkOption {
        type = types.nullOr (types.attrsOf types.str);
        default = null;
        description = "Environment variables scoped to this step.";
      };
    };
  };

  jobModule = { ... }: {
    freeformType = types.attrsOf types.anything;
    options = {
      runs-on = mkOption {
        type = types.either types.str (types.listOf types.str);
        default = "ubuntu-24.04";
        description = "Runner label(s) this job executes on.";
      };
      needs = mkOption {
        type = types.nullOr (types.either types.str (types.listOf types.str));
        default = null;
        description = "Job id(s) this job depends on; GitHub Actions waits for them and exposes their outputs.";
      };
      "if" = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "GitHub Actions expression gating whether this job runs.";
      };
      permissions = mkOption {
        type = types.nullOr (types.attrsOf types.str);
        default = null;
        example = {
          contents = "write";
        };
        description = "`GITHUB_TOKEN` permission scopes granted to this job.";
      };
      environment = mkOption {
        type = types.nullOr (types.attrsOf types.anything);
        default = null;
        example = {
          name = "github-pages";
        };
        description = "Deployment environment this job targets.";
      };
      concurrency = mkOption {
        type = types.nullOr (types.attrsOf types.anything);
        default = null;
        example = {
          group = "pages";
          cancel-in-progress = false;
        };
        description = "Concurrency group for this job, to serialize or cancel overlapping runs.";
      };
      strategy = mkOption {
        type = types.nullOr (types.attrsOf types.anything);
        default = null;
        example = {
          fail-fast = false;
          matrix.version = [
            "a"
            "b"
          ];
        };
        description = "Matrix/fail-fast strategy fanning this job out across multiple runs.";
      };
      outputs = mkOption {
        type = types.nullOr (types.attrsOf types.str);
        default = null;
        description = "Named outputs this job exposes to jobs that `need` it, as GitHub Actions expression strings.";
      };
      steps = mkOption {
        type = types.listOf (types.submodule stepModule);
        default = [ ];
        description = "Ordered steps this job runs.";
      };
    };
  };

  workflowModule = {
    options = {
      name = mkOption {
        type = types.str;
        description = "Workflow display name, shown in the Actions UI.";
      };
      on = mkOption {
        type = types.attrsOf types.anything;
        description = ''
          Trigger configuration, passed through verbatim -- shape varies
          too widely across trigger types to usefully type.
        '';
      };
      jobs = mkOption {
        type = types.attrsOf (types.submodule jobModule);
        default = { };
        description = ''
          Workflow jobs, keyed by job id. Order doesn't matter -- GitHub
          Actions schedules jobs from the `needs:` dependency graph, not
          declaration order.
        '';
      };
    };
  };

  # Recursively drops unset (null-default) option values so the rendered
  # YAML only contains keys a job/step actually set. The same trick
  # nixpkgs' own systemd module uses to turn unit-file option sets into INI
  # sections (`lib.filterAttrs (_: v: v != null)` in systemd/lib.nix's
  # attrsToSection) -- just applied recursively, since steps nest a list of
  # attrsets rather than a single flat one.
  #
  # Scoped to `jobs` only -- `on` is rendered verbatim, since a trigger
  # block can contain a deliberate `null` (e.g. a bare `workflow_dispatch`
  # with no inputs), which isn't an unset option.
  stripNulls =
    value:
    if builtins.isAttrs value then
      lib.mapAttrs (_: stripNulls) (lib.filterAttrs (_: v: v != null) value)
    else if builtins.isList value then
      map stripNulls value
    else
      value;

  # NixOS's own assertions/warnings option shape (nixos/modules/misc/
  # assertions.nix), vendored inline rather than imported from a nixpkgs
  # checkout path -- ghanix only ever needs `lib`, not a nixpkgs source
  # tree on disk. `lib.asserts.checkAssertWarn` below (used to check this
  # pair) is real, un-vendored nixpkgs library code; only this small
  # option-declaration pair is copied.
  assertionsModule = {
    options = {
      assertions = mkOption {
        type = types.listOf types.unspecified;
        internal = true;
        default = [ ];
        description = ''
          Conditions that must hold for workflow evaluation to succeed,
          along with the error messages to show when they don't.
        '';
      };
      warnings = mkOption {
        type = types.listOf types.str;
        internal = true;
        default = [ ];
        description = "Warnings to show during workflow evaluation.";
      };
    };
  };

  # A job with no steps is always an authoring mistake (a builder forgot to
  # set `steps`, or a conditional list ended up empty) -- expressed as a
  # module contributing to `config.assertions` from `config.jobs`, the same
  # pattern any real NixOS module uses to validate one option against
  # another.
  noEmptyJobsModule =
    { config, ... }:
    {
      assertions = [
        {
          assertion = lib.all (job: job.steps != [ ]) (lib.attrValues config.jobs);
          message =
            let
              empty = lib.attrNames (lib.filterAttrs (_: job: job.steps == [ ]) config.jobs);
            in
            "workflow '${config.name}': job(s) with no steps: ${lib.concatStringsSep ", " empty}";
        }
      ];
    };
in
{
  evalWorkflow =
    {
      name,
      on,
      jobs,
    }:
    let
      evaluated = lib.evalModules {
        modules = [
          assertionsModule
          workflowModule
          noEmptyJobsModule
          { inherit name on jobs; }
        ];
      };
      cfg = evaluated.config;
    in
    lib.asserts.checkAssertWarn cfg.assertions cfg.warnings {
      inherit (cfg) name on;
      jobs = stripNulls cfg.jobs;
    };
}
