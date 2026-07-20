# Shared GitHub Actions job builders.  This file is imported by the rendered
# workflow entrypoints; ci/render.py deliberately renders only on_*.nix.
{ }:
let
  ciLib = import ../lib.nix;
  inherit (ciLib) steps mkJob withCond;

  getFlake = builtins.${"getFlake"};
  flake = getFlake (toString ../../.);
  inherit (flake) lib;

  flakeTestOutputs = lib.pipe flake.packages.${builtins.currentSystem} [
    (lib.filterAttrs (_n: v: v.passthru.addToMatrix or false))
    lib.attrNames
    (map builtins.unsafeDiscardStringContext)
  ];

  nanopynixVersionNames = map (lib.removePrefix "nanopynix-tests-") flakeTestOutputs;
  regularVersionNames = builtins.filter (name: !lib.hasSuffix "-tsan" name) nanopynixVersionNames;
  tsanVersionNames = builtins.filter (lib.hasSuffix "-tsan") nanopynixVersionNames;

  # A single cachix/install-nix-action (multi-user) install suffices for every
  # job now: the test suite owns its own daemon and local store paths
  # entirely (see tests/support/nix_environment.py), so the CI runner's own
  # Nix install mode no longer affects what gets exercised. The remaining
  # local/daemon axis lives in `--nix-test-backends`, not in how Nix itself
  # was installed.
  mkTestSetup =
    {
      ref ? null,
      lockArtifact ? null,
    }:
    [ (steps.checkout { inherit ref; }) ]
    ++ ciLib.optional (lockArtifact != null) (steps.downloadArtifact { artifactName = lockArtifact; })
    ++ [
      (steps.installNix { })
      (steps.cachix { })
    ];

  mkRegularTestJob =
    {
      version,
      ref ? null,
      lockArtifact ? null,
      needs ? [ ],
    }:
    mkJob (
      ciLib.optionalAttrs (needs != [ ]) { inherit needs; }
      // {
        steps = mkTestSetup { inherit ref lockArtifact; } ++ [
          {
            name = "Build nanopynix test runner for Nix ${version}";
            run = ''nix build ".#nanopynix-tests-${version}" --out-link result --print-build-logs --print-out-paths'';
          }
          (steps.verifyClosure { name = "Verify test runner closure after build"; })
          (steps.enableSandboxNamespaces { })
          {
            name = "Test nanopynix against Nix ${version} (full suite, daemon backend)";
            run = # bash
              ''
                set -o pipefail
                paths_to_delete="''${{ github.workspace }}/nanopynix-test-store-paths.txt"
                rm -f "$paths_to_delete"
                status=0
                env NANOPYNIX_CORE_DEBUG=1 NANOPYNIX_GC_THREAD_DEBUG=1 NANOPYNIX_RPC_TIMEOUT=30 PYTHONDONTWRITEBYTECODE=1 COVERAGE_FILE=''${{ github.workspace }}/.coverage NANOPYNIX_TEST_DELETE_PATHS_FILE="$paths_to_delete" \
                  ./result/bin/nanopynix-tests --verbose --tb=short -rsxXfE --run-temp-store-builds --nix-test-backends daemon \
                  2>&1 | tee ''${{ github.workspace }}/test-gdb-output.log || status=$?
                if [ -s "$paths_to_delete" ]; then
                  nix store delete --stdin < "$paths_to_delete" || true
                fi
                exit "$status"
              '';
          }
          (steps.uploadArtifact {
            name = "Upload test output";
            artifactName = "test-output-${version}";
            path = "\${{ github.workspace }}/test-gdb-output.log";
          })
          (withCond "\${{ !cancelled() }}" {
            name = "Upload coverage reports to Codecov";
            uses = "codecov/codecov-action@main";
            "with" = {
              token = "\${{ secrets.CODECOV_TOKEN }}";
              files = "\${{ github.workspace }}/coverage.xml";
              flags = version;
            };
          })
          (withCond "\${{ !cancelled() }}" {
            name = "Upload test results to Codecov";
            uses = "codecov/codecov-action@main";
            "with" = {
              token = "\${{ secrets.CODECOV_TOKEN }}";
              files = "\${{ github.workspace }}/junit.xml";
              flags = version;
              report_type = "test_results";
            };
          })
          (steps.verifyClosure { name = "Verify test runner closure after tests"; })
        ];
      }
    );

  mkTsanTestJob =
    {
      version,
      ref ? null,
      lockArtifact ? null,
      needs ? [ ],
    }:
    let
      bareVersion = lib.removeSuffix "-tsan" version;
    in
    mkJob (
      ciLib.optionalAttrs (needs != [ ]) { inherit needs; }
      // {
        steps = mkTestSetup { inherit ref lockArtifact; } ++ [
          {
            name = "Build TSAN nanopynix test runner (${bareVersion})";
            run = ''nix build ".#nanopynix-tests-${version}" --out-link result --print-build-logs --print-out-paths'';
          }
          (steps.enableSandboxNamespaces { })
          {
            name = "Run TSAN-instrumented stress tests (repeated, local+daemon backends)";
            run = # bash
              ''
                set -o pipefail
                LOGFILE="''${{ github.workspace }}/tsan-output-${bareVersion}.log"
                race_found=0
                for i in $(seq 1 5); do
                  echo "=== TSAN run $i ===" | tee -a "$LOGFILE"
                  status=0
                  unshare --user --map-root-user --mount --pid --fork --mount-proc env NANOPYNIX_CORE_DEBUG=1 NANOPYNIX_RPC_TIMEOUT=30 NANOPYNIX_TEST_SANITIZER=tsan PYTHONDONTWRITEBYTECODE=1 \
                    ./result/bin/nanopynix-tests --verbose --tb=short -rsxXfE --capture=no --run-temp-store-builds --nix-test-backends local,daemon \
                    -m tsan_stress \
                    2>&1 | tee -a "$LOGFILE" || status=$?
                  echo "=== TSAN run $i exit status: $status ===" | tee -a "$LOGFILE"
                  if grep -q "ThreadSanitizer: data race" "$LOGFILE"; then
                    echo "TSAN data race detected on run $i -- stopping early" | tee -a "$LOGFILE"
                    race_found=1
                    break
                  fi
                done
                if [ "$race_found" -eq 1 ]; then
                  echo "::error::genuine ThreadSanitizer data race detected -- see log above"
                  exit 1
                fi
                if grep -qE "[0-9]+ (failed|error)|Fatal Python error|pthread_kill failed at suspend" "$LOGFILE"; then
                  echo "::error::pytest reported a real failure, or the process crashed -- see log above"
                  exit 1
                fi
                exit 0
              '';
          }
          {
            name = "Run TSAN-instrumented concurrency tests (single pass, local+daemon backends)";
            run = # bash
              ''
                set -o pipefail
                LOGFILE="''${{ github.workspace }}/tsan-output-broad-${bareVersion}.log"
                status=0
                unshare --user --map-root-user --mount --pid --fork --mount-proc env NANOPYNIX_CORE_DEBUG=1 NANOPYNIX_RPC_TIMEOUT=30 NANOPYNIX_TEST_SANITIZER=tsan PYTHONDONTWRITEBYTECODE=1 \
                  ./result/bin/nanopynix-tests --verbose --tb=short -rsxXfE --capture=no --run-temp-store-builds --nix-test-backends local,daemon -m concurrency \
                  2>&1 | tee -a "$LOGFILE" || status=$?
                echo "=== TSAN broad pass exit status: $status ===" | tee -a "$LOGFILE"
                if grep -q "ThreadSanitizer: data race" "$LOGFILE"; then
                  echo "::error::genuine ThreadSanitizer data race detected -- see log above"
                  exit 1
                fi
                if grep -qE "[0-9]+ (failed|error)|Fatal Python error|pthread_kill failed at suspend" "$LOGFILE"; then
                  echo "::error::pytest reported a real failure, or the process crashed -- see log above"
                  exit 1
                fi
                exit 0
              '';
          }
          (steps.uploadArtifact {
            name = "Upload TSAN output (${bareVersion})";
            artifactName = "tsan-race-report-${bareVersion}";
            path = "\${{ github.workspace }}/tsan-output-${bareVersion}.log\n\${{ github.workspace }}/tsan-output-broad-${bareVersion}.log\n";
          })
        ];
      }
    );

  mkDocsBuildJob =
    {
      needs,
      ref ? null,
      lockArtifact ? null,
    }:
    mkJob {
      inherit needs;
      steps =
        [ (steps.checkout { inherit ref; }) ]
        ++ ciLib.optional (lockArtifact != null) (steps.downloadArtifact { artifactName = lockArtifact; })
        ++ [
          (steps.installNix { })
          (steps.cachix { })
          {
            name = "Build documentation";
            run = "nix build .#nanopynix-docs --out-link result --print-build-logs --print-out-paths";
          }
          (steps.verifyClosure { name = "Verify docs closure"; })
          {
            name = "Prepare Pages artifact";
            run = ''
              mkdir -p public
              cp -r --no-preserve=mode,ownership result/. public/
            '';
          }
          {
            uses = "actions/upload-pages-artifact@main";
            "with" = { path = "public"; };
          }
        ];
    };

  mkDocsDeployJob =
    { needs }:
    mkJob {
      inherit needs;
      permissions = {
        pages = "write";
        id-token = "write";
      };
      environment = {
        name = "github-pages";
        url = "\${{ steps.deployment.outputs.page_url }}";
      };
      concurrency = {
        group = "pages";
        cancel-in-progress = false;
      };
      steps = [
        {
          name = "Deploy to GitHub Pages";
          id = "deployment";
          uses = "actions/deploy-pages@main";
        }
      ];
    };
in
{
  inherit
    ciLib
    regularVersionNames
    tsanVersionNames
    mkRegularTestJob
    mkTsanTestJob
    mkDocsBuildJob
    mkDocsDeployJob
    ;

  mkStaticTestJobs =
    {
      ref ? null,
      lockArtifact ? null,
      needs ? [ ],
    }:
    builtins.listToAttrs (
      map (version: {
        name = "test-${version}";
        value = mkRegularTestJob { inherit version ref lockArtifact needs; };
      }) regularVersionNames
    );

  mkStaticTsanTestJobs =
    {
      ref ? null,
      lockArtifact ? null,
      needs ? [ ],
    }:
    builtins.listToAttrs (
      map (version: {
        name = "test-tsan-${lib.removeSuffix "-tsan" version}";
        value = mkTsanTestJob { inherit version ref lockArtifact needs; };
      }) tsanVersionNames
    );
}
