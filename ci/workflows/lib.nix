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
  installModes = [ "single-user" "multi-user" ];

  mkTestSetup =
    {
      installMode,
      ref ? null,
      lockArtifact ? null,
    }:
    [ (steps.checkout { inherit ref; }) ]
    ++ ciLib.optional (lockArtifact != null) (steps.downloadArtifact { artifactName = lockArtifact; })
    ++ (
      if installMode == "single-user" then
        [ (steps.nixQuickInstall { }) ]
      else
        [ (steps.installNixMultiUser { }) ]
    )
    ++ [ (steps.cachix { }) ]
    ++ ciLib.optional (installMode == "single-user") (steps.configureSingleUserNix { });

  mkRegularTestJob =
    {
      version,
      installMode,
      ref ? null,
      lockArtifact ? null,
      needs ? [ ],
    }:
    mkJob (
      ciLib.optionalAttrs (needs != [ ]) { inherit needs; }
      // {
        steps = mkTestSetup { inherit installMode ref lockArtifact; } ++ [
          {
            name = "Build nanopynix test runner for Nix ${version}";
            run = ''nix build ".#nanopynix-tests-${version}" --out-link result --print-build-logs --print-out-paths'';
          }
          (steps.verifyClosure { name = "Verify test runner closure after build"; })
          (steps.enableSandboxNamespaces { })
          {
            name = "Test nanopynix against Nix ${version} (full suite)";
            run = # bash
              ''
                set -o pipefail
                paths_to_delete="''${{ github.workspace }}/nanopynix-test-store-paths.txt"
                rm -f "$paths_to_delete"
                status=0
                unshare --user --map-root-user --mount --pid --fork --mount-proc env NANOPYNIX_CORE_DEBUG=1 NANOPYNIX_GC_THREAD_DEBUG=1 NANOPYNIX_RPC_TIMEOUT=30 PYTHONDONTWRITEBYTECODE=1 COVERAGE_FILE=''${{ github.workspace }}/.coverage NANOPYNIX_TEST_DELETE_PATHS_FILE="$paths_to_delete" \
                  ./result/bin/nanopynix-tests --verbose --tb=short -rsxXfE --run-temp-store-builds \
                  2>&1 | tee ''${{ github.workspace }}/test-gdb-output.log || status=$?
                if [ -s "$paths_to_delete" ]; then
                  nix store delete --stdin < "$paths_to_delete" || true
                fi
                exit "$status"
              '';
          }
          (steps.uploadArtifact {
            name = "Upload test output";
            artifactName = "test-output-${version}-${installMode}";
            path = "\${{ github.workspace }}/test-gdb-output.log";
          })
          (withCond "\${{ !cancelled() }}" {
            name = "Upload coverage reports to Codecov";
            uses = "codecov/codecov-action@main";
            "with" = {
              token = "\${{ secrets.CODECOV_TOKEN }}";
              files = "\${{ github.workspace }}/coverage.xml";
              flags = "${version}-${installMode}";
            };
          })
          (withCond "\${{ !cancelled() }}" {
            name = "Upload test results to Codecov";
            uses = "codecov/codecov-action@main";
            "with" = {
              token = "\${{ secrets.CODECOV_TOKEN }}";
              files = "\${{ github.workspace }}/junit.xml";
              flags = "${version}-${installMode}";
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
      installMode,
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
        steps = mkTestSetup { inherit installMode ref lockArtifact; } ++ [
          {
            name = "Build TSAN nanopynix test runner (${bareVersion}, ${installMode})";
            run = ''nix build ".#nanopynix-tests-${version}" --out-link result --print-build-logs --print-out-paths'';
          }
          (steps.enableSandboxNamespaces { })
          {
            name = "Run TSAN-instrumented stress tests (repeated)";
            run = # bash
              ''
                set -o pipefail
                LOGFILE="''${{ github.workspace }}/tsan-output-${bareVersion}-${installMode}.log"
                race_found=0
                for i in $(seq 1 5); do
                  echo "=== TSAN run $i ===" | tee -a "$LOGFILE"
                  status=0
                  unshare --user --map-root-user --mount --pid --fork --mount-proc env NANOPYNIX_CORE_DEBUG=1 NANOPYNIX_RPC_TIMEOUT=30 PYTHONDONTWRITEBYTECODE=1 \
                    ./result/bin/nanopynix-tests --verbose --tb=short -rsxXfE --capture=no --run-temp-store-builds \
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
            name = "Run TSAN-instrumented concurrency tests (single pass)";
            run = # bash
              ''
                set -o pipefail
                LOGFILE="''${{ github.workspace }}/tsan-output-broad-${bareVersion}-${installMode}.log"
                status=0
                tsan_version="${version}"
                tsan_version="''${tsan_version%-tsan}"
                tsan_concurrency_selection="concurrency"
                if [ "${installMode}" = single-user ] && { [ "$tsan_version" = nix_2_34 ] || [ "$tsan_version" = nix_2_35 ]; }; then
                  # Nix master no longer crashes this LocalStore workload under TSAN.
                  tsan_concurrency_selection="concurrency and not known_nix_tsan_localstore_bug"
                fi
                unshare --user --map-root-user --mount --pid --fork --mount-proc env NANOPYNIX_CORE_DEBUG=1 NANOPYNIX_RPC_TIMEOUT=30 PYTHONDONTWRITEBYTECODE=1 \
                  ./result/bin/nanopynix-tests --verbose --tb=short -rsxXfE --capture=no --run-temp-store-builds -m "$tsan_concurrency_selection" \
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
            name = "Upload TSAN output (${bareVersion}, ${installMode})";
            artifactName = "tsan-race-report-${bareVersion}-${installMode}";
            path = "\${{ github.workspace }}/tsan-output-${bareVersion}-${installMode}.log\n\${{ github.workspace }}/tsan-output-broad-${bareVersion}-${installMode}.log\n";
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
          (steps.nixQuickInstall { })
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
    installModes
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
      builtins.concatMap (
        version:
        map (installMode: {
          name = "test-${version}-${installMode}";
          value = mkRegularTestJob { inherit version installMode ref lockArtifact needs; };
        }) installModes
      ) regularVersionNames
    );

  mkStaticTsanTestJobs =
    {
      ref ? null,
      lockArtifact ? null,
      needs ? [ ],
    }:
    builtins.listToAttrs (
      builtins.concatMap (
        version:
        map (installMode: {
          name = "test-tsan-${lib.removeSuffix "-tsan" version}-${installMode}";
          value = mkTsanTestJob { inherit version installMode ref lockArtifact needs; };
        }) installModes
      ) tsanVersionNames
    );
}
