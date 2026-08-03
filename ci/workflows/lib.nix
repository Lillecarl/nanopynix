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
  sanitizerSuffixes = [
    "-tsan"
    "-asan"
  ];
  isSanitized = name: lib.any (suffix: lib.hasSuffix suffix name) sanitizerSuffixes;
  # A sanitized variant is a separate job, never part of the regular matrix:
  # both are far slower, and neither collects coverage.
  regularVersionNames = builtins.filter (name: !isSanitized name) nanopynixVersionNames;
  tsanVersionNames = builtins.filter (lib.hasSuffix "-tsan") nanopynixVersionNames;
  asanVersionNames = builtins.filter (lib.hasSuffix "-asan") nanopynixVersionNames;

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

  # ASan roughly doubles the work, and this job builds nix, sqlite and
  # boehmgc instrumented before it runs anything. Twice the regular cap,
  # so the first scheduled run reports a number instead of a timeout.
  asanTimeoutMinutes = 60;

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

  # The gates of nix/checks.nix, in one job. They share a checkout and a Nix
  # install, they take about a minute between them, and Nix already builds
  # them in parallel -- so one job costs one runner and reports every gate.
  # `--keep-going` is what makes that last part true: without it the first
  # failing gate hides the rest.
  #
  # `check-grpclib-transports` is the odd one out, being a test run rather
  # than a static tool. It is here rather than in the `test-*` matrix because
  # it is version-independent: that matrix exists to run one suite against
  # each supported Nix version, and this library links no Nix at all, so
  # three copies of it would be three identical runs. See nix/checks.nix.
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
          name = "Run the gates (ruff, ruff-strict, ruff format, pyright, grpclib-transports)";
          run = ''
            nix build --no-link --print-build-logs --keep-going \
              ".#check-lint" ".#check-lint-strict" ".#check-format" ".#check-types" \
              ".#check-grpclib-transports"
          '';
        }
      ];
    };

  # The one part of the commit convention that a machine can check: the
  # Conventional Commits prefix that CLAUDE.md requires. It checks the *shape*
  # and not a list of allowed types, because CLAUDE.md gives `feat(scope):` and
  # `fix(tests):` as examples and never agreed a taxonomy. The shape alone is
  # enough to catch the subjects that this repository actually produced before
  # the convention settled -- `fmt`, `ASD-STE100`, `add gdb to devshell`.
  #
  # Needs no Nix, so it is the cheapest job in the workflow.
  #
  # DELIBERATELY NOT CHECKED. Read the name of this job as the subject line
  # only, because these two are not machine-decidable:
  #
  #   `Closes #<number>` is conditional. A commit completes an issue, or it
  #   does not, and no machine knows which. A required trailer would train
  #   people to write `Closes` for partial work -- the exact failure that
  #   CLAUDE.md warns about, and worse than no check.
  #
  #   The `Co-Authored-By` and `Claude-Session` trailers are contextual. A
  #   commit that a person writes without an agent carries neither, and it must
  #   not fail for that.
  mkCommitSubjectJob =
    {
      ref ? null,
      needs ? [ ],
    }:
    lib.optionalAttrs (needs != [ ]) { inherit needs; }
    // {
      timeout-minutes = 5;
      steps = [
        (steps.checkout {
          inherit ref;
          # The range below needs the commits themselves, and the default
          # checkout fetches one.
          fetchDepth = 0;
        })
        {
          name = "Check the Conventional Commits subject of each pushed commit";
          run = ''
            set -euo pipefail

            before="''${{ github.event.before }}"
            after="''${{ github.sha }}"

            # `before` is the all-zero SHA for a new branch, and it names a
            # commit that the remote no longer has after a force push. The
            # range means nothing in either case, so check the head alone.
            if git cat-file -e "$before^{commit}" 2>/dev/null; then
              commits=$(git rev-list --no-merges "$before..$after")
            else
              echo "no usable base commit; checking $after alone"
              commits=$(git rev-list --no-merges -1 "$after")
            fi

            # Say how many, so that a pass is auditable. A range with no
            # commits also exits 0, and a gate that quietly checks nothing
            # reads exactly like a gate that checked everything.
            echo "checking $(printf '%s\n' "$commits" | grep -c .) commit subject(s)"

            status=0
            for sha in $commits; do
              subject=$(git log -1 --format=%s "$sha")
              # A space is legal inside the parentheses, because this
              # repository writes a multi-scope subject as `(nanopynix, ekn)`.
              if ! printf '%s\n' "$subject" | grep -Eq '^[a-z]+(\([a-z0-9._/, -]+\))?!?: .+'; then
                echo "::error::$sha: not a Conventional Commits subject: $subject"
                status=1
              fi
            done

            if [ "$status" -ne 0 ]; then
              echo "CLAUDE.md requires the Conventional Commits prefix, for example 'feat(scope):' or 'fix(tests):'."
            fi
            exit "$status"
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
  # AddressSanitizer, and UndefinedBehaviorSanitizer riding along in the same
  # build. See nix/sanitizer.nix for why UBSan attaches here and not to TSAN.
  #
  # Scheduled only, deliberately. #35 says not to put this in the per-commit
  # workflow until the run time is known, and it is not known: ASan roughly
  # doubles a suite that already takes 8-13 minutes, so the cap below is twice
  # the regular one. The first scheduled run is the measurement.
  #
  # No coverage, and the `local` backend only. Coverage instrumentation costs
  # time this job has none of, and the daemon backend forks a handler process
  # per connection -- a shape worth a separate decision once the run time of
  # the simple case is a number rather than a guess.
  mkAsanTestJob =
    {
      version,
      ref ? null,
      lockArtifact ? null,
      needs ? [ ],
    }:
    let
      bareVersion = lib.removeSuffix "-asan" version;
    in
    lib.optionalAttrs (needs != [ ]) { inherit needs; }
    // {
      timeout-minutes = asanTimeoutMinutes;
      steps = mkTestSetup { inherit ref lockArtifact; } ++ [
        {
          name = "Build ASAN nanopynix test runner (${bareVersion})";
          run = ''nix build ".#nanopynix-tests-${version}" --out-link result --print-build-logs --print-out-paths'';
        }
        (steps.enableSandboxNamespaces { })
        {
          name = "Run ASAN/UBSAN-instrumented suite (${bareVersion}, local backend)";
          run = # bash
            ''
              set -o pipefail
              LOGFILE="''${{ github.workspace }}/asan-output-${bareVersion}.log"
              status=0
              env NANOPYNIX_CORE_DEBUG=1 NANOPYNIX_RPC_TIMEOUT=60 NANOPYNIX_TEST_SANITIZER=asan PYTHONDONTWRITEBYTECODE=1 \
                ./result/bin/nanopynix-tests --verbose --tb=short -rsxXfE --capture=no --run-temp-store-builds --nix-test-backends local \
                2>&1 | tee -a "$LOGFILE" || status=$?
              # A sanitizer report is the finding, so grep for it rather than
              # trusting the exit status alone: abort_on_error=1 makes ASan
              # kill the process, but a report raised inside a forkserver
              # worker can still be reaped into a plain test failure.
              if grep -qE "(AddressSanitizer|UndefinedBehaviorSanitizer|runtime error):" "$LOGFILE"; then
                echo "::error::sanitizer report on ${bareVersion} -- see the log above"
                exit 1
              fi
              exit "$status"
            '';
        }
        (steps.uploadArtifact {
          name = "Upload ASAN output (${bareVersion})";
          artifactName = "asan-report-${bareVersion}";
          path = "\${{ github.workspace }}/asan-output-${bareVersion}.log";
        })
      ];
    };

  inherit
    evalWorkflow
    steps
    withCond
    regularVersionNames
    regularBackends
    tsanVersionNames
    asanVersionNames
    mkRegularTestJob
    mkTsanTestJob
    mkStaticChecksJob
    mkCommitSubjectJob
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
