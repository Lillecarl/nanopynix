let
  workflow = import ./lib.nix { };
  inherit (workflow) ciLib;
  inherit (ciLib) steps;

  branch = "develop";
  lockArtifact = "flake-lock";
  updateJob = "update-lockfile";

  versionExpression = output: "\${{ fromJson(needs.${updateJob}.outputs.${output}) }}";

  # One job per kind, each expanded across Nix versions via a real GHA
  # matrix -- there's no longer an install-mode axis to fan out over (see
  # ci/workflows/lib.nix), just the version.
  allTestJobs = {
    test-regular =
      workflow.mkRegularTestJob {
        version = "\${{ matrix.version }}";
        backend = "\${{ matrix.backend }}";
        ref = branch;
        lockArtifact = lockArtifact;
        needs = [ updateJob ];
      }
      // {
        strategy = {
          fail-fast = false;
          matrix = {
            version = versionExpression "regular_versions";
            backend = workflow.regularBackends;
          };
        };
      };

    test-tsan =
      workflow.mkTsanTestJob {
        version = "\${{ matrix.version }}";
        ref = branch;
        lockArtifact = lockArtifact;
        needs = [ updateJob ];
      }
      // {
        strategy = {
          fail-fast = false;
          matrix = {
            version = versionExpression "tsan_versions";
          };
        };
      };
  };
in
workflow.evalWorkflow {
  name = "On schedule";
  on = {
    schedule = [ { cron = "17 3 * * *"; } ];
    workflow_dispatch = null;
  };
  jobs = {
    update-lockfile = {
      outputs = {
        regular_versions = "\${{ steps.versions.outputs.regular_versions }}";
        tsan_versions = "\${{ steps.versions.outputs.tsan_versions }}";
      };
      steps = [
        (steps.checkout { ref = branch; })
        (steps.installNix { })
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
    update-lockfile-commit = {
      needs = "docs-deploy";
      permissions = {
        contents = "write";
      };
      steps = [
        (steps.checkout { ref = branch; })
        (steps.downloadArtifact { artifactName = lockArtifact; })
        {
          uses = "step-security/git-auto-commit-action@main";
          "with" = {
            commit_message = "nix flake update";
          };
        }
      ];
    };
  };
}
