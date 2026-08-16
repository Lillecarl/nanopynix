let
  workflow = import ./lib.nix { };
  inherit (workflow) withCond;

  testJobs = workflow.mkStaticTestJobs { };
  tsanTestJobs = workflow.mkStaticTsanTestJobs { };
  ubsanTestJobs = workflow.mkStaticUbsanTestJobs { };
  # The build with no collector, and the build under ASAN, on every commit.
  # `ci/workflows/lib.nix` gives the measurement that earns each slot.
  nogcTestJobs = workflow.mkStaticNoGCTestJobs { };
  asanTestJobs = workflow.mkStaticAsanTestJobs { };
  # One macOS job, on the floor version and the daemon backend. `lib.nix` says
  # why this one, why the backend is `daemon` and not `local`, and why it
  # carries `continue-on-error`. Issue #143.
  darwinTestJob = {
    test-darwin-nix_2_34 = workflow.mkDarwinTestJob {
      version = "nix_2_34";
      backend = "daemon";
    };
  };
  # Named alongside the test jobs so the `jobs` dispatch input can select it,
  # and so a docs deploy waits for it. It is the cheapest job in the workflow.
  staticChecksJob = {
    static-checks = workflow.mkStaticChecksJob { };
  };
  # Same reasoning, and cheaper still: it installs no Nix.
  commitSubjectJob = {
    commit-subjects = workflow.mkCommitSubjectJob { };
  };
  # The jobs a docs deploy waits for. **The macOS job is not one of them.**
  # GitHub skips a job whose `needs` failed, and `continue-on-error` stops the
  # run from failing without making the job succeed, so a red macOS job would
  # take the docs deploy with it. That is the shape that already cost a deploy
  # once, when `commit-subjects` went red on develop. Move the job in here
  # when it is green and `continue-on-error` comes off.
  gatingTestJobs =
    staticChecksJob
    // commitSubjectJob
    // testJobs
    // tsanTestJobs
    // ubsanTestJobs
    // nogcTestJobs
    // asanTestJobs;
  # Everything the `jobs` dispatch input can name, which does include macOS.
  allTestJobs = gatingTestJobs // darwinTestJob;
  selectedTestJobs = builtins.mapAttrs (
    name: job:
    withCond "github.event_name != 'workflow_dispatch' || inputs.jobs == '' || contains(format(',{0},', inputs.jobs), ',${name},')" job
  ) allTestJobs;
in
workflow.evalWorkflow {
  name = "On commit";
  env = workflow.workflowEnv;
  on = {
    # Keep ci-develop available as a pushed ref for focused workflow_dispatch
    # runs without starting the full push matrix.
    push = {
      branches-ignore = [ "ci-develop" ];
    };
    workflow_dispatch = {
      inputs = {
        jobs = {
          description = "Comma-separated job IDs to run; leave empty for the full matrix";
          required = false;
          type = "string";
          default = "";
        };
      };
    };
  };
  jobs = selectedTestJobs // {
    docs-build = withCond "github.ref == 'refs/heads/develop'" (
      workflow.mkDocsBuildJob {
        needs = builtins.attrNames gatingTestJobs;
      }
    );
    docs-deploy = withCond "github.ref == 'refs/heads/develop'" (
      workflow.mkDocsDeployJob {
        needs = "docs-build";
      }
    );
  };
}
