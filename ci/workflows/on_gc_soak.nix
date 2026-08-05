# TEMPORARY, for issue #70. Delete this file with the issue.
#
# #70 reproduces only in the whole suite, at about 1 failure in 5 runs of 8
# minutes. That rate needs repetition, and repetition is what CI is for.
#
# Three arms, five runs each, every job independent. The failure count of each
# arm is the measurement.
#
# - control     -- the shipped configuration, and the rate to compare against.
# - interior    -- every displacement valid. `GC_push_contents_hdr` consults
#                  `GC_valid_offsets` and black-lists a reference at a
#                  displacement Boehm does not know, rather than marking its
#                  object. Nix registers 1 to 7. A clean arm here says an
#                  unregistered displacement is the cause; a dirty one
#                  excludes that whole class.
# - amplified   -- `GC_FREE_SPACE_DIVISOR=64`, so the collector runs far more
#                  often. The rate of #70 already follows the collection rate,
#                  measured on the soak: 0 in 20 with `GC_DONT_GC=1`, 1 in 30
#                  by default, 5 in 15 here. This arm says whether the full
#                  suite answers the same knob.
#
# `docs/collector-and-threads.md` records what each arm settles.
let
  workflow = import ./lib.nix { };
  inherit (workflow)
    steps
    caps
    mkJob
    ;

  version = "nix_2_34";
in
workflow.evalWorkflow {
  name = "GC soak";
  # A push to `gc-soak` starts the measurement. GitHub registers a workflow for
  # `workflow_dispatch` only after that workflow reaches the default branch,
  # and this one stays off the default branch, so a push is the trigger that
  # is available. The branch is its own, so a push to `ci-develop` does not
  # start 15 jobs of 30 minutes each.
  on = {
    push = {
      branches = [ "gc-soak" ];
    };
  };
  jobs = {
    gc-soak = mkJob {
      strategy = {
        fail-fast = false;
        matrix = {
          arm = [
            "control"
            "interior"
            "amplified"
          ];
          run = [
            1
            2
            3
            4
            5
          ];
          # `include` adds a key to each combination that already matches, so
          # every `arm` gets its own environment without a `case` in bash.
          include = [
            {
              arm = "control";
              armEnv = "NANOPYNIX_GC_SOAK_ARM=control";
            }
            {
              arm = "interior";
              armEnv = "NANOPYNIX_GC_ALL_INTERIOR_POINTERS=1";
            }
            {
              arm = "amplified";
              armEnv = "GC_FREE_SPACE_DIVISOR=64";
            }
          ];
        };
      };
      steps =
        [
          # No `ref`: the checkout takes the branch that the push carried.
          (steps.checkout { })
          (steps.installNix { })
          (steps.cachix { })
        ]
        ++ [
          {
            name = "Build the nanopynix test runner for Nix ${version}";
            timeout-minutes = caps.build;
            run = ''nix build ".#nanopynix-tests-${version}" --out-link result --print-build-logs'';
          }
          (steps.enableSandboxNamespaces { })
          {
            name = "Run the full suite (arm \${{ matrix.arm }}, run \${{ matrix.run }})";
            timeout-minutes = caps.suite;
            run = # bash
              ''
                set -o pipefail
                env NANOPYNIX_CORE_DEBUG=1 NANOPYNIX_RPC_TIMEOUT=30 PYTHONDONTWRITEBYTECODE=1 \
                  ''${{ matrix.armEnv }} \
                  ./result/bin/nanopynix-tests --verbose --tb=short -rsxXfE --run-temp-store-builds \
                  --nix-test-backends local \
                  -m "not soak"
              '';
          }
        ];
    };
  };
}
