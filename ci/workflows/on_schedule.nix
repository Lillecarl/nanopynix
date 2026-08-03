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

    # The two builds against a libexpr with no collector. Scheduled only, and
    # `ci/workflows/lib.nix` says why: neither has run yet, so the caps are
    # guesses and issue #35 asks that the per-commit workflow wait for a
    # number. `test-nogc` is what tells a red `test-asan` apart from an
    # evaluator that does not work without the collector.
    test-nogc =
      workflow.mkNoGCTestJob {
        version = "\${{ matrix.version }}";
        ref = branch;
        inherit lockArtifact;
        needs = [ updateJob ];
      }
      // {
        strategy = {
          fail-fast = false;
          matrix = {
            version = versionExpression "nogc_versions";
          };
        };
      };

    test-asan =
      workflow.mkAsanTestJob {
        version = "\${{ matrix.version }}";
        ref = branch;
        inherit lockArtifact;
        needs = [ updateJob ];
      }
      // {
        strategy = {
          fail-fast = false;
          matrix = {
            version = versionExpression "asan_versions";
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
      # One output for each variant suffix, plus the regular matrix.
      # `ci/workflows/lib.nix` builds both this and the step that fills it
      # from one list, so a new variant reaches the scheduled workflow without
      # an edit here.
      outputs = workflow.versionMatrixOutputs;
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
          # The trailing newline keeps the rendered block a plain `|` rather
          # than a `|-`. Same shell either way; a stable render is what the
          # staleness gate in tests/nanopynix/test_ci_workflows.py compares.
          run = builtins.concatStringsSep "\n" (workflow.versionMatrixLines ++ [ "" ]);
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
