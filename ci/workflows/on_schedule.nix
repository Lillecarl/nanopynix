let
  workflow = import ./lib.nix { };
  inherit (workflow)
    steps
    caps
    mkJob
    withTimeout
    ;

  branch = "develop";
  lockArtifact = "flake-lock";
  updateJob = "update-lockfile";

  versionExpression = output: "\${{ fromJson(needs.${updateJob}.outputs.${output}) }}";

  # One job per kind, each expanded across Nix versions via a real GHA
  # matrix -- there's no longer an install-mode axis to fan out over (see
  # ci/workflows/lib.nix), just the version.
  allTestJobs = {
    static-checks = workflow.mkStaticChecksJob {
      ref = branch;
      inherit lockArtifact;
      needs = [ updateJob ];
    };

    test-regular =
      workflow.mkRegularTestJob {
        version = "\${{ matrix.version }}";
        backend = "\${{ matrix.backend }}";
        ref = branch;
        inherit lockArtifact;
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

    test-ubsan =
      workflow.mkUbsanTestJob {
        version = "\${{ matrix.version }}";
        ref = branch;
        inherit lockArtifact;
        needs = [ updateJob ];
      }
      // {
        strategy = {
          fail-fast = false;
          matrix = {
            version = versionExpression "ubsan_versions";
          };
        };
      };

    test-tsan =
      workflow.mkTsanTestJob {
        version = "\${{ matrix.version }}";
        ref = branch;
        inherit lockArtifact;
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
    update-lockfile = mkJob {
      outputs = {
        regular_versions = "\${{ steps.versions.outputs.regular_versions }}";
        tsan_versions = "\${{ steps.versions.outputs.tsan_versions }}";
        ubsan_versions = "\${{ steps.versions.outputs.ubsan_versions }}";
      };
      steps = [
        (steps.checkout { ref = branch; })
        (steps.installNix { })
        (steps.cachix { })
        {
          name = "Update flake inputs";
          timeout-minutes = caps.flakeUpdate;
          run = "nix flake update";
        }
        {
          id = "versions";
          name = "Compute Nix version matrices";
          timeout-minutes = caps.versionMatrix;
          run = ''
            echo "regular_versions=$(nix eval --json '.#packages.x86_64-linux' --apply 'pkgs: map (builtins.replaceStrings ["nanopynix-tests-"] [""]) (builtins.filter (n: builtins.match "nanopynix-tests-.*" n != null && builtins.match ".*-(tsan|ubsan)" n == null) (builtins.attrNames pkgs))')" >> "$GITHUB_OUTPUT"
            echo "tsan_versions=$(nix eval --json '.#packages.x86_64-linux' --apply 'pkgs: map (builtins.replaceStrings ["nanopynix-tests-"] [""]) (builtins.filter (n: builtins.match "nanopynix-tests-.*" n != null && builtins.match ".*-tsan" n != null) (builtins.attrNames pkgs))')" >> "$GITHUB_OUTPUT"
            echo "ubsan_versions=$(nix eval --json '.#packages.x86_64-linux' --apply 'pkgs: map (builtins.replaceStrings ["nanopynix-tests-"] [""]) (builtins.filter (n: builtins.match "nanopynix-tests-.*" n != null && builtins.match ".*-ubsan" n != null) (builtins.attrNames pkgs))')" >> "$GITHUB_OUTPUT"
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
      inherit lockArtifact;
    };
    docs-deploy = workflow.mkDocsDeployJob { needs = "docs-build"; };
    update-lockfile-commit = mkJob {
      needs = "docs-deploy";
      permissions = {
        contents = "write";
      };
      steps = [
        (steps.checkout { ref = branch; })
        (steps.downloadArtifact { artifactName = lockArtifact; })
        (withTimeout caps.autoCommit {
          uses = "step-security/git-auto-commit-action@main";
          "with" = {
            commit_message = "nix flake update";
          };
        })
      ];
    };
  };
}
