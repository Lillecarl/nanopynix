let
  workflow = import ./lib.nix { };
  inherit (workflow.ciLib) mkWorkflow withCond;

  testJobs = workflow.mkStaticTestJobs { };
  tsanTestJobs = workflow.mkStaticTsanTestJobs { };
  allTestJobs = testJobs // tsanTestJobs;
in
mkWorkflow {
  name = "On commit";
  on = {
    # Keep ci-develop available as a pushed ref for focused workflow_dispatch
    # runs without starting the full push matrix.
    push = { branches-ignore = [ "ci-develop" ]; };
    workflow_dispatch = null;
  };
  jobs = allTestJobs // {
    docs-build = withCond "github.ref == 'refs/heads/develop'" (workflow.mkDocsBuildJob {
      needs = builtins.attrNames allTestJobs;
    });
    docs-deploy = withCond "github.ref == 'refs/heads/develop'" (workflow.mkDocsDeployJob {
      needs = "docs-build";
    });
  };
}
