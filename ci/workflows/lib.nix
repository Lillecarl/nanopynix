# Shared GitHub Actions job builders.  This file is imported by the rendered
# workflow entrypoints; ci/render.py deliberately renders only on_*.nix.
{ }:
let
  getFlake = builtins.${"getFlake"};
  flake = getFlake (toString ../../.);
  inherit (flake) lib;

  ghalib = import ../../ghanix { inherit lib; };
  inherit (ghalib) steps withCond evalWorkflow;

  flakeTestOutputs = lib.pipe flake.packages.${builtins.currentSystem} [
    (lib.filterAttrs (_n: v: v.passthru.addToMatrix or false))
    lib.attrNames
    (map builtins.unsafeDiscardStringContext)
  ];

  nanopynixVersionNames = map (lib.removePrefix "nanopynix-tests-") flakeTestOutputs;
  regularVersionNames = builtins.filter (name: !lib.hasSuffix "-tsan" name) nanopynixVersionNames;
  tsanVersionNames = builtins.filter (lib.hasSuffix "-tsan") nanopynixVersionNames;

  # Coverage-collecting backends run as separate matrix jobs (test-daemon-*,
  # test-local-*) rather than serially inside one job, so covering both stays
  # roughly free in wall-clock: they run in parallel. TSAN already exercises
  # local+daemon together in its own repeated stress runs, but deliberately
  # without coverage instrumentation (see mkTsanTestJob).
  regularBackends = [
    "daemon"
    "local"
  ];

  # Every test job here finishes well inside this: the regular suites take
  # 8-13 minutes and the TSAN ones about 4. The cap exists for the case that
  # is not a slow job but a stopped one -- twice now a daemon job has hung on
  # a forkserver child that never reported, and GitHub's unset default let it
  # sit for 117 and 145 minutes before a human noticed and cancelled it. A
  # hang that runs to a timeout is still a failed job, but it fails in half an
  # hour and it fails visibly.
  testTimeoutMinutes = 30;

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
    ++ lib.optional (lockArtifact != null) (steps.downloadArtifact { artifactName = lockArtifact; })
    ++ [
      (steps.installNix { })
      (steps.cachix { })
    ];

  mkRegularTestJob =
    {
      version,
      backend,
      ref ? null,
      lockArtifact ? null,
      needs ? [ ],
    }:
    lib.optionalAttrs (needs != [ ]) { inherit needs; }
    // {
      # See testTimeoutMinutes -- a suite that hangs must not cost six hours.
      timeout-minutes = testTimeoutMinutes;
      steps = mkTestSetup { inherit ref lockArtifact; } ++ [
        {
          name = "Build nanopynix test runner for Nix ${version}";
          run = ''nix build ".#nanopynix-tests-${version}" --out-link result --print-build-logs --print-out-paths'';
        }
        (steps.verifyClosure { name = "Verify test runner closure after build"; })
        (steps.enableSandboxNamespaces { })
        {
          name = "Test nanopynix against Nix ${version} (full suite, ${backend} backend)";
          run = # bash
            ''
              set -o pipefail
              paths_to_delete="''${{ github.workspace }}/nanopynix-test-store-paths.txt"
              rm -f "$paths_to_delete"
              # Not stderr: pytest captures fd 2 per test by default and only
              # prints the buffer when that test fails, so a SIGSEGV takes the
              # GC thread log down with it -- a full run that crashed produced
              # zero diagnostic lines. A file is outside pytest's capture.
              gc_thread_log="''${{ github.workspace }}/gc-thread-debug.log"
              rm -f "$gc_thread_log"
              status=0
              # NANOPYNIX_COVERAGE rather than pytest-cov's --cov: the runner
              # then measures with `coverage run` and combines after pytest
              # exits. pytest-cov combines *inside* the run, alongside live
              # evaluator threads and forkserver workers, and that combine
              # intermittently failed with "database is locked" -- exit 3 on a
              # job whose tests had all passed. See nanopynix/tests.nix.
              env NANOPYNIX_CORE_DEBUG=1 NANOPYNIX_GC_THREAD_DEBUG=1 NANOPYNIX_GC_THREAD_DEBUG_FILE="$gc_thread_log" NANOPYNIX_RPC_TIMEOUT=30 PYTHONDONTWRITEBYTECODE=1 COVERAGE_FILE=''${{ github.workspace }}/.coverage NANOPYNIX_COVERAGE=1 NANOPYNIX_COVERAGE_XML=''${{ github.workspace }}/coverage.xml NANOPYNIX_TEST_DELETE_PATHS_FILE="$paths_to_delete" \
                ./result/bin/nanopynix-tests --verbose --tb=short -rsxXfE --run-temp-store-builds --nix-test-backends ${backend} \
                --junitxml=''${{ github.workspace }}/junit.xml \
                2>&1 | tee ''${{ github.workspace }}/test-gdb-output.log || status=$?
              # Only on a crash: this file has one line per evaluator thread
              # registration and is long, so it is worth the log space solely
              # when there is a faulting LWP to correlate it against.
              if [ "$status" -gt 128 ] && [ -s "$gc_thread_log" ]; then
                {
                  echo "=== Boehm GC thread registration log ($(wc -l <"$gc_thread_log") lines) ==="
                  cat "$gc_thread_log"
                } >> ''${{ github.workspace }}/test-gdb-output.log
              fi
              if [ -s "$paths_to_delete" ]; then
                nix store delete --stdin < "$paths_to_delete" || true
              fi
              exit "$status"
            '';
        }
        (steps.uploadArtifact {
          name = "Upload test output";
          artifactName = "test-output-${backend}-${version}";
          path = "\${{ github.workspace }}/test-gdb-output.log";
        })
        # Uploaded on every run, not just a crash. The step above inlines this
        # into the log only when pytest died of a signal, which turned out to
        # be the wrong trigger: the same suspected evaluator-state corruption
        # also surfaces as an ordinary *test failure* (a value of the wrong
        # type reaching nixpkgs' `env` type check, exit 1), and that path
        # needs the same registration history to correlate against. As an
        # artifact it costs no log space.
        (steps.uploadArtifact {
          name = "Upload Boehm GC thread registration log";
          artifactName = "gc-thread-debug-${backend}-${version}";
          path = "\${{ github.workspace }}/gc-thread-debug.log";
        })
        (withCond "\${{ !cancelled() }}" {
          name = "Upload coverage reports to Codecov";
          uses = "codecov/codecov-action@main";
          "with" = {
            token = "\${{ secrets.CODECOV_TOKEN }}";
            files = "\${{ github.workspace }}/coverage.xml";
            flags = "${backend}-${version}";
          };
        })
        (withCond "\${{ !cancelled() }}" {
          name = "Upload test results to Codecov";
          uses = "codecov/codecov-action@main";
          "with" = {
            token = "\${{ secrets.CODECOV_TOKEN }}";
            files = "\${{ github.workspace }}/junit.xml";
            flags = "${backend}-${version}";
            report_type = "test_results";
          };
        })
        (steps.verifyClosure { name = "Verify test runner closure after tests"; })
      ];
    };

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
    lib.optionalAttrs (needs != [ ]) { inherit needs; }
    // {
      # See testTimeoutMinutes -- a suite that hangs must not cost six hours.
      timeout-minutes = testTimeoutMinutes;
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
    };

  # The four static gates of nix/checks.nix, in one job. They share a checkout
  # and a Nix install, they take about a minute between them, and Nix already
  # builds the four in parallel -- so one job costs one runner and reports
  # every gate. `--keep-going` is what makes that last part true: without it
  # the first failing gate hides the other three.
  mkStaticChecksJob =
    {
      ref ? null,
      lockArtifact ? null,
      needs ? [ ],
    }:
    lib.optionalAttrs (needs != [ ]) { inherit needs; }
    // {
      timeout-minutes = 20;
      steps = [
        (steps.checkout { inherit ref; })
      ]
      ++ lib.optional (lockArtifact != null) (steps.downloadArtifact { artifactName = lockArtifact; })
      ++ [
        (steps.installNix { })
        (steps.cachix { })
        {
          name = "Run the static gates (ruff, ruff-strict, ruff format, pyright)";
          run = ''
            nix build --no-link --print-build-logs --keep-going \
              ".#check-lint" ".#check-lint-strict" ".#check-format" ".#check-types"
          '';
        }
      ];
    };

  mkDocsBuildJob =
    {
      needs,
      ref ? null,
      lockArtifact ? null,
    }:
    {
      inherit needs;
      steps = [
        (steps.checkout { inherit ref; })
      ]
      ++ lib.optional (lockArtifact != null) (steps.downloadArtifact { artifactName = lockArtifact; })
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
          "with" = {
            path = "public";
          };
        }
      ];
    };

  mkDocsDeployJob =
    { needs }:
    {
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
    evalWorkflow
    steps
    withCond
    regularVersionNames
    regularBackends
    tsanVersionNames
    mkRegularTestJob
    mkTsanTestJob
    mkStaticChecksJob
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
        backend:
        map (version: {
          name = "test-${backend}-${version}";
          value = mkRegularTestJob {
            inherit
              version
              backend
              ref
              lockArtifact
              needs
              ;
          };
        }) regularVersionNames
      ) regularBackends
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
        value = mkTsanTestJob {
          inherit
            version
            ref
            lockArtifact
            needs
            ;
        };
      }) tsanVersionNames
    );
}
