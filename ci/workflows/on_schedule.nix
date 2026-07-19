let
  workflow = import ./lib.nix { };
  inherit (workflow) ciLib installModes;
  inherit (ciLib) mkJob mkWorkflow steps;

  branch = "develop";
  lockArtifact = "flake-lock";
  updateJob = "update-lockfile";

  versionExpression = output: "\${{ fromJson(needs.${updateJob}.outputs.${output}) }}";

  mkScheduledJobs =
    {
      kind,
      output,
      mkJobForMode,
    }:
    builtins.listToAttrs (
      map (installMode: {
        name = "test-${kind}-${installMode}";
        value = (mkJobForMode installMode) // {
          strategy = {
            fail-fast = false;
            matrix = { version = versionExpression output; };
          };
        };
      }) installModes
    );

  regularJobs = mkScheduledJobs {
    kind = "regular";
    output = "regular_versions";
    mkJobForMode = installMode: workflow.mkRegularTestJob {
      version = "\${{ matrix.version }}";
      inherit installMode;
      ref = branch;
      lockArtifact = lockArtifact;
      needs = [ updateJob ];
    };
  };

  tsanJobs = mkScheduledJobs {
    kind = "tsan";
    output = "tsan_versions";
    mkJobForMode = installMode: workflow.mkTsanTestJob {
      version = "\${{ matrix.version }}";
      inherit installMode;
      ref = branch;
      lockArtifact = lockArtifact;
      needs = [ updateJob ];
    };
  };

  allTestJobs = regularJobs // tsanJobs;
in
mkWorkflow {
  name = "On schedule";
  on = {
    schedule = [ { cron = "17 3 * * *"; } ];
    workflow_dispatch = null;
  };
  jobs = {
    update-lockfile = mkJob {
      outputs = {
        regular_versions = "\${{ steps.versions.outputs.regular_versions }}";
        tsan_versions = "\${{ steps.versions.outputs.tsan_versions }}";
      };
      steps = [
        (steps.checkout { ref = branch; })
        (steps.nixQuickInstall { })
        (steps.cachix { })
        {
          name = "Update flake inputs";
          run = "nix flake update";
        }
        {
          id = "versions";
          name = "Compute Nix version matrices";
          run = ''
            echo "regular_versions=$(nix eval --json '.#packages.x86_64-linux' --apply 'pkgs: map (builtins.replaceStrings ["nanopynix-tests-"] [""]) (builtins.filter (n: builtins.match "nanopynix-tests-.*" n != null && builtins.match ".*-tsan" n == null) (builtins.attrNames pkgs))')" >> "$GITHUB_OUTPUT"
            echo "tsan_versions=$(nix eval --json '.#packages.x86_64-linux' --apply 'pkgs: map (builtins.replaceStrings ["nanopynix-tests-"] [""]) (builtins.filter (n: builtins.match ".*-tsan" n != null) (builtins.attrNames pkgs))')" >> "$GITHUB_OUTPUT"
          '';
        }
        (steps.uploadArtifact {
          name = null;
          artifactName = lockArtifact;
          path = "flake.lock";
          cond = null;
        })
      ];
    };
  }
  // allTestJobs
  // {
    docs-build = workflow.mkDocsBuildJob {
      needs = builtins.attrNames allTestJobs;
      ref = branch;
      lockArtifact = lockArtifact;
    };
    docs-deploy = workflow.mkDocsDeployJob { needs = "docs-build"; };
    update-lockfile-commit = mkJob {
      needs = "docs-deploy";
      permissions = { contents = "write"; };
      steps = [
        (steps.checkout { ref = branch; })
        (steps.downloadArtifact { artifactName = lockArtifact; })
        {
          uses = "step-security/git-auto-commit-action@main";
          "with" = { commit_message = "nix flake update"; };
        }
      ];
    };
  };
}
