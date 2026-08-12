let
  workflow = import ./lib.nix { };
  inherit (workflow)
    steps
    caps
    mkJob
    withCond
    withTimeout
    ;

  # **A dispatch can select one job, and a scheduled run always takes them
  # all.** `on_commit.nix` carries the same expression, and the wheel jobs are
  # why this workflow needs it too: each one builds the whole closure, so a run
  # to test one of them must not start the entire nightly.
  #
  # `update-lockfile` never takes this condition. Every job below needs it for
  # the flake it tests and for the version matrix it reads.
  dispatchable = builtins.mapAttrs (
    name: job:
    withCond "github.event_name != 'workflow_dispatch' || inputs.jobs == '' || contains(format(',{0},', inputs.jobs), ',${name},')" job
  );

  # **A dispatch tests the branch that it names, and a scheduled run tests
  # `develop`.** `github.ref_name` is the default branch on a scheduled run,
  # and the default branch of this repository is `develop`, so the schedule
  # keeps the behaviour that the literal gave it.
  #
  # A dispatch needs the other half. The job selection above lets you run one
  # job of this workflow, and a checkout of `develop` then builds code that the
  # job under test does not have. That is how the wheel jobs arrived: neither
  # one can run before it reaches `develop`, and each one costs hours.
  branch = "\${{ github.ref_name }}";
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

    # The two builds against a libexpr with no collector. `test-nogc` runs on
    # every commit as well, because it has the measurement that issue #35 asks
    # for; `test-asan` is scheduled only, because it has none.
    # `ci/workflows/lib.nix` carries both halves of that reasoning.
    #
    # It stays here beside `test-asan` even so, and not only for symmetry: the
    # scheduled run tests a freshly updated flake, and it is what tells a red
    # `test-asan` apart from an evaluator that does not work without the
    # collector at all.
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
  env = workflow.workflowEnv;
  on = {
    schedule = [ { cron = "17 3 * * *"; } ];
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
        # **This runs after `nix flake update`, and that is the whole point.**
        # The script is built from the updated flake, so the version list it
        # writes is the updated one. `ci/steps.nix` says why that replaced five
        # `nix eval` calls that each repeated the variant suffixes as a regular
        # expression.
        (
          workflow.mkNixRunStep {
            name = "Compute Nix version matrices";
            attr = "version-matrix";
            cap = caps.versionMatrix;
          }
          // {
            id = "versions";
          }
        )
        (steps.uploadArtifact {
          name = null;
          artifactName = lockArtifact;
          path = "flake.lock";
          cond = null;
        })
      ];
    };
  }
  // dispatchable (
    allTestJobs
    // {
      # **The wheel, on each architecture, and here rather than on every
      # commit.** This build compiles the whole closure with a compiler wrapper
      # of its own, so it shares no derivation with the test jobs and cachix
      # holds none of it until this job has run. `ci/workflows/lib.nix` gives the
      # measurement behind the cap.
      #
      # A scheduled run also tests a freshly updated flake, which is what this
      # job most needs: three of the four defects of issue #120 came from the
      # toolchain under it, and a bumped nixpkgs is how the next one arrives.
      wheel-x86_64 = workflow.mkWheelJob {
        ref = branch;
        inherit lockArtifact;
        needs = [ updateJob ];
      };

      # **A native arm64 runner, and never emulation.** GitHub supplies
      # `ubuntu-24.04-arm` to a public repository, and this host registers binfmt
      # for aarch64, so an x86-64 runner would silently build the whole closure
      # under qemu.
      #
      # It runs the smoke test as well, because the runner is the architecture of
      # the wheel. That is the one thing the community builder cannot give: a
      # foreign-architecture container does not start on the machine that builds
      # the aarch64 wheel by hand today.
      wheel-aarch64 = workflow.mkWheelJob {
        runner = "ubuntu-24.04-arm";
        ref = branch;
        inherit lockArtifact;
        needs = [ updateJob ];
      };

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
    }
  );
}
