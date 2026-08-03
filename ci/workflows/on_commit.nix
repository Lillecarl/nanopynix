let
  workflow = import ./lib.nix { };
  inherit (workflow) withCond;

  testJobs = workflow.mkStaticTestJobs { };
  tsanTestJobs = workflow.mkStaticTsanTestJobs { };
  ubsanTestJobs = workflow.mkStaticUbsanTestJobs { };
  # Named alongside the test jobs so the `jobs` dispatch input can select it,
  # and so a docs deploy waits for it. It is the cheapest job in the workflow.
  staticChecksJob = {
    static-checks = workflow.mkStaticChecksJob { };
  };
  # Same reasoning, and cheaper still: it installs no Nix.
  commitSubjectJob = {
    commit-subjects = workflow.mkCommitSubjectJob { };
  };
  allTestJobs = staticChecksJob // commitSubjectJob // testJobs // tsanTestJobs // ubsanTestJobs;
  selectedTestJobs = builtins.mapAttrs (
    name: job:
    withCond "github.event_name != 'workflow_dispatch' || inputs.jobs == '' || contains(format(',{0},', inputs.jobs), ',${name},')" job
  ) allTestJobs;
in
workflow.evalWorkflow {
  name = "On commit";
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
        needs = builtins.attrNames allTestJobs;
      }
    );
    docs-deploy = withCond "github.ref == 'refs/heads/develop'" (
      workflow.mkDocsDeployJob {
        needs = "docs-build";
      }
    );
  };
}
