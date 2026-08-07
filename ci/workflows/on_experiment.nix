# The generic experiment runner.
#
# **This workflow exists so that a new experiment costs no workflow file.**
# GitHub makes a workflow available to `workflow_dispatch` only after that
# workflow reaches the default branch, so each new YAML file costs a push to
# `develop` before it can run once. This file pays that cost one time. After
# it lands, a new experiment is an attribute in `ci/experiments.nix` on any
# branch, and a dispatch that names the attribute and the branch.
#
# The three inputs that make one workflow cover every shape:
#
# - `ref`        -- the branch to check out, so the experiment does not have
#                   to reach `develop` either.
# - `experiment` -- the attribute of `ci/experiments.nix` to run.
# - `matrix`     -- the fan-out, as JSON. `fromJSON` turns the string into the
#                   `strategy.matrix` of the job, so the number of arms and
#                   the number of runs are dispatch parameters.
#
# The experiment reads its own cell from `MATRIX_JSON`, and the runner passes
# no other argument. Neither input reaches the shell body of a step: both go
# through the environment, so a value cannot become a command.
#
# Dispatch it:
#
#   gh workflow run on_experiment.yml \
#     -f ref=ci-develop \
#     -f experiment=gc-soak \
#     -f matrix='{"arm":["control","interior","amplified"],"run":[1,2,3,4,5]}'
let
  workflow = import ./lib.nix { };
  inherit (workflow)
    steps
    caps
    mkJob
    ;
in
workflow.evalWorkflow {
  name = "Experiment";
  env = workflow.workflowEnv;
  on = {
    workflow_dispatch = {
      inputs = {
        experiment = {
          description = "Attribute of ci/experiments.nix to run";
          required = true;
          type = "string";
        };
        ref = {
          description = "Branch, tag, or FULL 40-character sha to check out";
          required = false;
          type = "string";
          default = "ci-develop";
        };
        matrix = {
          description = ''JSON matrix, for example {"arm":["a","b"],"run":[1,2]}'';
          required = false;
          type = "string";
          default = ''{"run":[1]}'';
        };
        runner = {
          description = "Runner to use";
          required = false;
          type = "choice";
          options = [
            "ubuntu-24.04"
            "ubuntu-24.04-arm"
          ];
          default = "ubuntu-24.04";
        };
      };
    };
  };
  jobs = {
    experiment = mkJob {
      runs-on = "\${{ inputs.runner }}";
      strategy = {
        # One failed cell must not stop the others: the failure count over the
        # whole matrix is the measurement.
        fail-fast = false;
        matrix = "\${{ fromJSON(inputs.matrix) }}";
      };
      steps = [
        # **Two checkouts, and the first one is the guard.**
        #
        # `ci-check-dispatch-ref` refuses an abbreviated sha, which is a trap
        # worth three minutes of a runner: `actions/checkout` reads a `ref`
        # that is not a full 40-character sha as a branch or tag name, so
        # `ref=59b837c26769` becomes `refs/heads/59b837c26769`, git retries,
        # and it fails with "exit code 1" and no explanation. It cost a whole
        # 30-job measurement once.
        #
        # The guard is a package now, so it needs the tree that holds it. This
        # first checkout takes the ref the workflow was dispatched from, which
        # always exists, and the second takes the requested one. A checkout
        # costs seconds, and the guard still fires before the retry it exists
        # to prevent.
        (steps.checkout { })
        (steps.installNix { })
        (workflow.mkNixRunStep {
          name = "Check the ref";
          attr = "check-dispatch-ref";
          cap = 10;
          env = {
            REF = "\${{ inputs.ref }}";
          };
        })
        (steps.checkout { ref = "\${{ inputs.ref }}"; })
        (steps.cachix { })
        # Every experiment here drives the Nix test suite, which needs the
        # sandbox namespaces and a core pattern that a later step can collect.
        (workflow.mkSandboxStep { })
        {
          name = "Run \${{ inputs.experiment }}";
          timeout-minutes = caps.build + caps.suite;
          env = {
            EXPERIMENT = "\${{ inputs.experiment }}";
            MATRIX_JSON = "\${{ toJSON(matrix) }}";
          };
          run = ''nix run --file . "experiments.$EXPERIMENT"'';
        }
        (steps.uploadArtifact {
          name = "Keep the evidence of a failed cell";
          # `job.status` and the matrix cell together make each upload unique,
          # which `actions/upload-artifact` requires within one run.
          artifactName = "experiment-\${{ inputs.experiment }}-\${{ strategy.job-index }}";
          path = ''
            /tmp/core.*
            .pytest-agent/
          '';
          cond = "\${{ failure() }}";
        })
      ];
    };
  };
}
