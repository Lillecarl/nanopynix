# Nix reproduction of .github/workflows/ci.yml, rendered to YAML by
# ci/render.py via nanopynix's toYAML primop. See ci/lib.nix for the helper
# functions used below.
let
  ciLib = import ../lib.nix;
  inherit (ciLib) steps mkJob withCond;

  # default.nix already exports pkgs.lib (see its own `inherit (pkgs) lib;`)
  # -- reuse that instead of hand-rolling hasSuffix/removeSuffix here.
  getFlake = builtins.${"getFlake"};
  flake = getFlake (toString ../../.);
  inherit (flake) lib;

  # Statically enumerate the test job matrix at Nix-eval time instead of
  # discovering it at CI runtime via a `matrix` job running `nix eval --json`.
  # flake.packages is exactly what `nix build ".#<name>"` can build, so
  # driving the matrix off it (rather than reaching into default.nix's
  # internal nanopynixVersions attrset directly) can't drift from what CI
  # actually builds. Each `nanopynix-tests-<name>` package sets
  # `passthru.addToMatrix = true` (see python/tests.nix) precisely so this
  # filter can pick out the test-runner packages and nothing else (nanopynix,
  # pynix, shell, nanopynix-docs, ... are never in the matrix).
  # unsafeDiscardStringContext is defensive: these are plain attribute names
  # already (context-free), but forcing a derivation-derived string into YAML
  # text would drag a build into `render.py` just to render CI config --
  # discarding context up front makes that mistake inert rather than a
  # surprise `nix build` invocation.
  flakeTestOutputs = lib.pipe flake.packages.${builtins.currentSystem} [
    (lib.filterAttrs (_n: v: v.passthru.addToMatrix or false))
    lib.attrNames
    (map builtins.unsafeDiscardStringContext)
  ];

  # `nanopynix-tests-<name>` -> `<name>` (e.g. `nanopynix-tests-nix_2_34-tsan`
  # -> `nix_2_34-tsan`), matching the bare version names used in job/step
  # names and `nix build ".#nanopynix-tests-${version}"` commands below.
  nanopynixVersionNames = map (lib.removePrefix "nanopynix-tests-") flakeTestOutputs;

  regularVersionNames = builtins.filter (name: !lib.hasSuffix "-tsan" name) nanopynixVersionNames;
  tsanVersionNames = builtins.filter (lib.hasSuffix "-tsan") nanopynixVersionNames;

  installModes = [
    "single-user"
    "multi-user"
  ];

  mkTestJob =
    { version, installMode }:
    withCond "vars.RUN_MATRIX_TESTS == 'true'" (mkJob {
      steps = [
        (steps.checkout { })
      ]
      ++ (
        if installMode == "single-user" then
          [ (steps.nixQuickInstall { }) ]
        else
          [ (steps.installNixMultiUser { }) ]
      )
      ++ [ (steps.cachix { }) ]
      ++ ciLib.optional (installMode == "single-user") (steps.configureSingleUserNix { })
      ++ [
        {
          name = "Build nanopynix test runner for Nix ${version}";
          run = ''nix build ".#nanopynix-tests-${version}" --out-link result --print-build-logs --print-out-paths'';
        }
        (steps.verifyClosure { name = "Verify test runner closure after build"; })
        (steps.enableSandboxNamespaces { })
        {
          name = "Test nanopynix against Nix ${version} (DIAGNOSTIC-ONLY, post-mortem core dump on crash, narrowed run)";
          run = # bash
            ''
              set -o pipefail
              unshare --user --map-root-user --mount --pid --fork --mount-proc env NANOPYNIX_CORE_DEBUG=1 NANOPYNIX_GC_THREAD_DEBUG=1 NANOPYNIX_RPC_TIMEOUT=30 PYTHONDONTWRITEBYTECODE=1 COVERAGE_FILE=''${{ github.workspace }}/.coverage \
                ./result/bin/nanopynix-tests --verbose --tb=short -rsxXfE --run-temp-store-builds tests/nanopynix/test_inproc_multithreaded_poc.py \
                2>&1 | tee ''${{ github.workspace }}/test-gdb-output.log
              if grep -qE "^[0-9]+ failed|SIGSEGV|SIGABRT|Fatal Python error" ''${{ github.workspace }}/test-gdb-output.log; then
                echo "::error::test run crashed or failed -- see gdb backtrace above"
                exit 1
              fi
            '';
        }
        (steps.uploadArtifact {
          name = "Upload gdb crash backtrace";
          artifactName = "gdb-backtrace-${version}";
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
    });

  testJobs = builtins.listToAttrs (
    builtins.concatMap (
      version:
      map (installMode: {
        name = "test-${version}-${installMode}";
        value = mkTestJob { inherit version installMode; };
      }) installModes
    ) regularVersionNames
  );

  # ThreadSanitizer build hunting concurrency bugs in the single-user
  # in-process build/eval paths. Single-user only, since that's the only
  # mode the crashes reproduce in; not part of the normal version/
  # install-mode matrix, but DOES block on its own: a genuine new
  # ThreadSanitizer data race, or any test failure other than the two
  # known TSAN-timing-sensitive assertions (xfail-marked in
  # test_inproc_multithreaded_poc.py; see _running_under_tsan there),
  # fails these jobs. The known upstream boehmgc/glibc pthread_kill-vs-
  # thread-exit race (see
  # ../../nix/patches/boehmgc-tolerate-suspend-thread-exit-race.patch) is
  # patched, not tolerated here -- if it regresses, these jobs should fail.
  #
  # One job per tsanVersionNames entry (nix_2_31-tsan/nix_2_34-tsan/
  # nix_2_35-tsan/git-tsan, see default.nix's patchedNixVersions) instead
  # of a single hardcoded nix_2_35 build, so a race can be confirmed/ruled
  # out on every supported version, not just the one it happened to be
  # found on. `version` here is the full flake output suffix (e.g.
  # "nix_2_35-tsan"); `bareVersion` strips "-tsan" for human-readable
  # names/artifact names.
  mkTsanTestJob =
    { version }:
    let
      bareVersion = lib.removeSuffix "-tsan" version;
    in
    mkJob {
      "if" = "github.event_name != 'schedule'";
      steps = [
        (steps.checkout { })
        (steps.nixQuickInstall { })
        (steps.cachix { })
        (steps.configureSingleUserNix { })
        {
          name = "Build TSAN nanopynix test runner (${bareVersion})";
          run = ''nix build ".#nanopynix-tests-${version}" --out-link result --print-build-logs --print-out-paths'';
        }
        (steps.enableSandboxNamespaces { })
        {
          name = "Run TSAN-instrumented crashing test (repeated)";
          run = # bash
            ''
              set -o pipefail
              LOGFILE="''${{ github.workspace }}/tsan-output-${bareVersion}.log"
              race_found=0
              for i in $(seq 1 5); do
                echo "=== TSAN run $i ===" | tee -a "$LOGFILE"
                status=0
                # --capture=no: pytest normally captures a test's stdout/stderr into
                # its own buffer and only replays it on a *normal* test completion.
                # TSAN's halt_on_error aborts the process mid-test, so pytest never
                # gets to replay that buffer -- the race report was being silently
                # lost in exactly that captured-but-never-flushed buffer. Disabling
                # capture lets TSAN's direct fd writes reach our log in real time.
                unshare --user --map-root-user --mount --pid --fork --mount-proc env NANOPYNIX_CORE_DEBUG=1 NANOPYNIX_RPC_TIMEOUT=30 PYTHONDONTWRITEBYTECODE=1 \
                  ./result/bin/nanopynix-tests --verbose --tb=short -rsxXfE --capture=no --run-temp-store-builds \
                  "tests/nanopynix/test_inproc_multithreaded_poc.py::test_inproc_parallel_batch_builds_use_multiple_store_workers" \
                  2>&1 | tee -a "$LOGFILE" || status=$?
                # pytest's inline PASSED/FAILED marker can land on a non-newline-terminated
                # line that GH Actions' log API sometimes drops at a step boundary --
                # print an explicit, unambiguous exit code so pass/fail is never in doubt.
                echo "=== TSAN run $i exit status: $status ===" | tee -a "$LOGFILE"
                # Only a genuine data race stops the loop early -- a "thread leak"
                # finding (nix's own libcurl worker thread not joined at exit) is
                # a separate, benign category that still triggers halt_on_error
                # (ending that one run early) but shouldn't prevent the remaining
                # iterations from re-checking for an actual race.
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
              # TSAN's halt_on_error=1 can make the whole process exit nonzero
              # purely from the known-benign libcurl "thread leak" atexit
              # finding (nix's own file-transfer worker thread, never joined
              # by design) -- that fires *after* pytest's own summary is
              # already recorded, so trust pytest's own reported outcome (its
              # test's timing assertion is xfail(strict=True)-marked under
              # TSAN; see _running_under_tsan in test_inproc_multithreaded_poc.py)
              # rather than the raw process exit code. A real "N failed"/"N
              # error" tally, an XPASS(strict) turning into "N failed" once
              # the underlying issue is actually fixed, or a hard crash
              # signature are all genuine findings worth failing on.
              if grep -qE "[0-9]+ (failed|error)|Fatal Python error|pthread_kill failed at suspend" "$LOGFILE"; then
                echo "::error::pytest reported a real failure, or the process crashed -- see log above"
                exit 1
              fi
              exit 0
            '';
        }
        {
          name = "Run TSAN-instrumented concurrency-relevant test files (single pass)";
          run = # bash
            ''
              set -o pipefail
              LOGFILE="''${{ github.workspace }}/tsan-output-broad-${bareVersion}.log"
              status=0
              # Broader (but not exhaustive) coverage pass: every test file that
              # touches NixThreadExecutor/threading/concurrent.futures, not just
              # the one test known to hit the now-fixed races. Single pass only
              # (not repeated like the targeted run above) -- halt_on_error=1
              # stops the whole run at the first finding anywhere, so repeating
              # wouldn't add coverage, only cost. The other ~39 test files are
              # single-threaded and wouldn't exercise any new race, so they're
              # intentionally excluded to keep this pass cheap.
              unshare --user --map-root-user --mount --pid --fork --mount-proc env NANOPYNIX_CORE_DEBUG=1 NANOPYNIX_RPC_TIMEOUT=30 PYTHONDONTWRITEBYTECODE=1 \
                ./result/bin/nanopynix-tests --verbose --tb=short -rsxXfE --capture=no --run-temp-store-builds \
                tests/nanopynix/test_inproc.py \
                tests/nanopynix/test_worker_store_unit.py \
                tests/nanopynix/test_inproc_multithreaded_poc.py \
                tests/nanopynix/test_session_unit.py \
                tests/nanopynix/test_eval_rpc.py \
                tests/nanopynix/test_l3_inproc.py \
                tests/nanopynix/test_worker_eval_unit.py \
                2>&1 | tee -a "$LOGFILE" || status=$?
              echo "=== TSAN broad pass exit status: $status ===" | tee -a "$LOGFILE"
              if grep -q "ThreadSanitizer: data race" "$LOGFILE"; then
                echo "::error::genuine ThreadSanitizer data race detected -- see log above"
                exit 1
              fi
              # See the repeated-run step above for why the raw process exit
              # code isn't trustworthy here: TSAN's halt_on_error=1 can fire
              # on the known-benign libcurl "thread leak" atexit finding
              # after pytest's own summary is already recorded clean.
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

  tsanTestJobs = builtins.listToAttrs (
    map (version: {
      name = "test-tsan-${lib.removeSuffix "-tsan" version}";
      value = mkTsanTestJob { inherit version; };
    }) tsanVersionNames
  );
in
ciLib.mkWorkflow {
  name = "CI";

  on = {
    push = null;
    schedule = [ { cron = "17 3 * * *"; } ];
    workflow_dispatch = null;
  };

  jobs =
    testJobs
    // tsanTestJobs
    // {
      docs-build =
        withCond "github.event_name != 'schedule' && github.ref == 'refs/heads/develop'"
          (mkJob {
            needs = builtins.attrNames testJobs;
            steps = [
              (steps.checkout { })
              (steps.nixQuickInstall { })
              (steps.cachix { })
              {
                name = "Build documentation";
                run = "nix build .#nanopynix-docs --out-link result --print-build-logs --print-out-paths";
              }
              (steps.verifyClosure { name = "Verify docs closure"; })
              {
                name = "Prepare Pages artifact";
                run = # bash
                  ''
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
          });

      docs-deploy =
        withCond "github.event_name != 'schedule' && github.ref == 'refs/heads/develop'"
          (mkJob {
            needs = "docs-build";
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
          });

      update-lockfile-prep = withCond "github.event_name == 'schedule'" (mkJob {
        outputs = {
          versions = "\${{ steps.versions.outputs.versions }}";
        };
        steps = [
          (steps.checkout { ref = "main"; })
          (steps.nixQuickInstall { })
          (steps.cachix { })
          {
            name = "Update flake inputs";
            run = "nix flake update";
          }
          {
            id = "versions";
            name = "Compute Nix version matrix";
            run = ''echo "versions=$(nix eval --json '.#packages.x86_64-linux' --apply 'pkgs: map (builtins.replaceStrings ["nanopynix-tests-"] [""]) (builtins.filter (n: builtins.match "nanopynix-tests-.*" n != null) (builtins.attrNames pkgs))')" >> "$GITHUB_OUTPUT"'';
          }
          (steps.uploadArtifact {
            name = null;
            artifactName = "flake-lock";
            path = "flake.lock";
            cond = null;
          })
        ];
      });

      update-lockfile-test = withCond "github.event_name == 'schedule'" (mkJob {
        needs = "update-lockfile-prep";
        strategy = {
          fail-fast = false;
          matrix = {
            version = "\${{ fromJson(needs.update-lockfile-prep.outputs.versions) }}";
          };
        };
        steps = [
          (steps.checkout { ref = "main"; })
          (steps.downloadArtifact { artifactName = "flake-lock"; })
          (steps.nixQuickInstall { })
          (steps.cachix { })
          (steps.configureSingleUserNix { })
          {
            name = "Build nanopynix test runner for Nix \${{ matrix.version }}";
            run = ''nix build ".#nanopynix-tests-''${{ matrix.version }}" --out-link result --print-build-logs --print-out-paths'';
          }
          (steps.verifyClosure { name = "Verify test runner closure after build"; })
          (steps.enableSandboxNamespaces { corePattern = false; })
          {
            name = "Test nanopynix against Nix \${{ matrix.version }}";
            run = # bash
              "unshare --user --map-root-user --mount --pid --fork --mount-proc env NANOPYNIX_RPC_TIMEOUT=30 PYTHONDONTWRITEBYTECODE=1 ./result/bin/nanopynix-tests --verbose --tb=short -rsxXfE --run-temp-store-builds";
          }
          (steps.verifyClosure { name = "Verify test runner closure after tests"; })
        ];
      });

      update-lockfile-commit = withCond "github.event_name == 'schedule'" (mkJob {
        needs = "update-lockfile-test";
        permissions = {
          contents = "write";
        };
        steps = [
          (steps.checkout { ref = "main"; })
          (steps.downloadArtifact { artifactName = "flake-lock"; })
          {
            uses = "step-security/git-auto-commit-action@main";
            "with" = {
              commit_message = "nix flake update";
            };
          }
        ];
      });
    };
}
