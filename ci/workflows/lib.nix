# Shared GitHub Actions job builders.  This file is imported by the rendered
# workflow entrypoints; ci/render.py deliberately renders only on_*.nix.
{ }:
let
  getFlake = builtins.${"getFlake"};
  flake = getFlake (toString ../../.);
  inherit (flake) lib;

  ghalib = import ../../ghanix { inherit lib; };
  inherit (ghalib)
    steps
    withCond
    withTimeout
    evalWorkflow
    ;

  flakeTestOutputs = lib.pipe flake.packages.${builtins.currentSystem} [
    (lib.filterAttrs (_n: v: v.passthru.addToMatrix or false))
    lib.attrNames
    (map builtins.unsafeDiscardStringContext)
  ];

  nanopynixVersionNames = map (lib.removePrefix "nanopynix-tests-") flakeTestOutputs;
  sanitizerSuffixes = [
    "-tsan"
    "-ubsan"
  ];
  isSanitized = name: lib.any (suffix: lib.hasSuffix suffix name) sanitizerSuffixes;
  # A sanitized variant is a separate job, never part of the regular matrix:
  # both are far slower, and neither collects coverage.
  regularVersionNames = builtins.filter (name: !isSanitized name) nanopynixVersionNames;
  tsanVersionNames = builtins.filter (lib.hasSuffix "-tsan") nanopynixVersionNames;
  ubsanVersionNames = builtins.filter (lib.hasSuffix "-ubsan") nanopynixVersionNames;

  # Coverage-collecting backends run as separate matrix jobs (test-daemon-*,
  # test-local-*) rather than serially inside one job, so covering both stays
  # roughly free in wall-clock: they run in parallel. TSAN already exercises
  # local+daemon together in its own repeated stress runs, but deliberately
  # without coverage instrumentation (see mkTsanTestJob).
  regularBackends = [
    "daemon"
    "local"
  ];

  # A cap for each step that this file writes, in minutes. `ghanix/steps.nix`
  # carries the caps of the steps that it builds.
  #
  # **The cap belongs to the step, and not to the job.** A cap exists for the
  # case that is not a slow step but a stopped one -- twice a daemon job has
  # hung on a forkserver child that never reported, and GitHub's unset default
  # let it sit for 117 and 145 minutes before a person cancelled it. One cap
  # for a whole job can only hold the sum, so the slack of the longest step
  # reaches every other step: a sanitized job that builds for 40 minutes and
  # then tests for 10 needs a cap of 50, and a hung suite under that cap runs
  # for 40 minutes before it stops. A cap for each step gives the build the
  # time that the build needs, and it holds the suite to the time that the
  # suite needs.
  #
  # This also answers a real cost. A change to `nix/sanitizer.nix` rebuilds
  # the instrumented closure, which takes 25 minutes for the TSAN variant and
  # 38 for the UBSan one, and one 30-minute job cap stopped two TSAN jobs for
  # that reason alone (run 30782379867). The build step now holds that number
  # by itself, and the test steps keep the tight cap that makes a hang visible.
  #
  # Each number is generous against a measurement, and each measurement is
  # named beside it.
  caps = {
    # `nix build` of the test runner. cachix holds the closure, so this is a
    # fetch and a build of the five packages of this repository: 2.3 minutes.
    # The cap holds a cold build of Nix itself, which a bumped nixpkgs causes.
    build = 30;
    # The same build, instrumented. Cold, and measured: 25 minutes for TSAN,
    # 38 for UBSan on the slowest version. A change to nix/sanitizer.nix is
    # the only thing that makes it cold.
    tsanBuild = 45;
    ubsanBuild = 60;
    # The full suite. It takes 8 to 13 minutes on every version and backend,
    # and 9.7 to 13 under UBSan (run 30782379867).
    suite = 30;
    # Five repeated runs of the tsan_stress selection, about 4 minutes
    # together, and one pass over the concurrency selection.
    tsanStress = 25;
    tsanBroad = 20;
    # `nix build` of five gates, which take about a minute between them.
    staticChecks = 20;
    # One `git rev-list` and one `grep` for each commit pushed. No Nix.
    commitSubjects = 5;
    # The documentation build, and the copy of its output into `public/`.
    docsBuild = 30;
    docsPrepare = 5;
    # An upload of the whole site, and then a wait on GitHub Pages. Both are
    # out of our hands.
    docsUpload = 15;
    docsDeploy = 20;
    # An upload to Codecov of one XML file.
    codecov = 10;
    # `nix flake update`, which fetches every input, and one `nix eval` for
    # each of the three version matrices. The eval is cold: `nix flake update`
    # just moved every input.
    flakeUpdate = 20;
    versionMatrix = 20;
    # One commit and one push, by an action.
    autoCommit = 10;
  };

  # The cap of a job, derived from the caps of its steps.
  #
  # GitHub applies both caps, and the smaller one wins. A job cap is therefore
  # not a second opinion about how long the work takes; it is a backstop for
  # the time that belongs to no step. Derive it, so that a raised step cap
  # cannot leave a job cap behind that silently overrides it.
  #
  # The sum of the caps is a sum of worst cases, so a derived job cap is much
  # larger than any run. That is correct for a backstop: each step already
  # holds its own time, and the job cap only has to catch what no step covers.
  #
  # `jobSlack` is that uncovered time. The post phase of an action is the real
  # case: `cachix/cachix-action` pushes the paths that the job built after the
  # last step ends, and no step cap reaches it.
  jobSlack = 15;
  mkJob =
    job:
    let
      capOf =
        step:
        step.timeout-minutes or (throw ''
          ci/workflows: this step declares no timeout-minutes, so the cap of
          its job cannot be derived. Give it one from the `caps` table in
          ci/workflows/lib.nix, with `withTimeout`. The step was:
          ${builtins.toJSON step}
        '');
    in
    job
    // {
      timeout-minutes = lib.foldl' (total: step: total + capOf step) jobSlack job.steps;
    };

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
    mkJob (
      lib.optionalAttrs (needs != [ ]) { inherit needs; }
      // {
        steps = mkTestSetup { inherit ref lockArtifact; } ++ [
          {
            name = "Build nanopynix test runner for Nix ${version}";
            timeout-minutes = caps.build;
            run = ''nix build ".#nanopynix-tests-${version}" --out-link result --print-build-logs --print-out-paths'';
          }
          (steps.verifyClosure { name = "Verify test runner closure after build"; })
          (steps.enableSandboxNamespaces { })
          {
            name = "Test nanopynix against Nix ${version} (full suite, ${backend} backend)";
            timeout-minutes = caps.suite;
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
          (withCond "\${{ !cancelled() }}" (
            withTimeout caps.codecov {
              name = "Upload coverage reports to Codecov";
              uses = "codecov/codecov-action@main";
              "with" = {
                token = "\${{ secrets.CODECOV_TOKEN }}";
                files = "\${{ github.workspace }}/coverage.xml";
                flags = "${backend}-${version}";
              };
            }
          ))
          (withCond "\${{ !cancelled() }}" (
            withTimeout caps.codecov {
              name = "Upload test results to Codecov";
              uses = "codecov/codecov-action@main";
              "with" = {
                token = "\${{ secrets.CODECOV_TOKEN }}";
                files = "\${{ github.workspace }}/junit.xml";
                flags = "${backend}-${version}";
                report_type = "test_results";
              };
            }
          ))
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
      lib.optionalAttrs (needs != [ ]) { inherit needs; }
      // {
        steps = mkTestSetup { inherit ref lockArtifact; } ++ [
          {
            name = "Build TSAN nanopynix test runner (${bareVersion})";
            timeout-minutes = caps.tsanBuild;
            run = ''nix build ".#nanopynix-tests-${version}" --out-link result --print-build-logs --print-out-paths'';
          }
          (steps.enableSandboxNamespaces { })
          {
            name = "Run TSAN-instrumented stress tests (repeated, local+daemon backends)";
            timeout-minutes = caps.tsanStress;
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
            timeout-minutes = caps.tsanBroad;
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

  # The full suite under UndefinedBehaviorSanitizer, against one Nix version.
  #
  # UBSan runs on its own rather than beside TSAN, although the two combine.
  # The TSAN matrix skips 2.31, and 2.31 is the one version where the
  # ownership rules that UBSan is here to check differ. See nix/sanitizer.nix.
  #
  # There is no AddressSanitizer job, and Nix decides that: libexpr refuses
  # the combination of ASAN and the collector. Issue #47 holds the supported
  # route to ASAN, which is a worker process that runs without the collector.
  #
  # No coverage, and the `local` backend only. Coverage instrumentation costs
  # time this job has none of, and the daemon backend forks a handler process
  # per connection -- a shape worth a separate decision once the run time of
  # the simple case is a number rather than a guess.
  mkUbsanTestJob =
    {
      version,
      ref ? null,
      lockArtifact ? null,
      needs ? [ ],
    }:
    let
      bareVersion = lib.removeSuffix "-ubsan" version;
    in
    mkJob (
      lib.optionalAttrs (needs != [ ]) { inherit needs; }
      // {
        steps = mkTestSetup { inherit ref lockArtifact; } ++ [
          {
            name = "Build UBSAN nanopynix test runner (${bareVersion})";
            timeout-minutes = caps.ubsanBuild;
            run = ''nix build ".#nanopynix-tests-${version}" --out-link result --print-build-logs --print-out-paths'';
          }
          (steps.enableSandboxNamespaces { })
          {
            name = "Run UBSAN-instrumented suite (${bareVersion}, local backend)";
            timeout-minutes = caps.suite;
            run = # bash
              ''
                set -o pipefail
                LOGFILE="''${{ github.workspace }}/ubsan-output-${bareVersion}.log"
                status=0
                env NANOPYNIX_CORE_DEBUG=1 NANOPYNIX_RPC_TIMEOUT=60 NANOPYNIX_TEST_SANITIZER=ubsan PYTHONDONTWRITEBYTECODE=1 \
                  ./result/bin/nanopynix-tests --verbose --tb=short -rsxXfE --capture=no --run-temp-store-builds --nix-test-backends local \
                  2>&1 | tee -a "$LOGFILE" || status=$?
                # A sanitizer report is the finding, so grep for it rather than
                # trusting the exit status alone: halt_on_error=1 makes UBSan
                # kill the process, but a report raised inside a forkserver
                # worker can still be reaped into a plain test failure.
                #
                # "Unexpected condition" is the message of `nix::unreachable`,
                # which is what `nixUnreachableWhenHardened` becomes once
                # `NIX_UBSAN_ENABLED` is on. That path never prints the word
                # "runtime error", so the first two patterns would miss it.
                if grep -qE "(UndefinedBehaviorSanitizer|runtime error):|Unexpected condition in " "$LOGFILE"; then
                  echo "::error::sanitizer report on ${bareVersion} -- see the log above"
                  exit 1
                fi
                exit "$status"
              '';
          }
          (steps.uploadArtifact {
            name = "Upload UBSAN output (${bareVersion})";
            artifactName = "ubsan-report-${bareVersion}";
            path = "\${{ github.workspace }}/ubsan-output-${bareVersion}.log";
          })
        ];
      }
    );

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
    mkJob (
      lib.optionalAttrs (needs != [ ]) { inherit needs; }
      // {
        steps = [
          (steps.checkout { inherit ref; })
        ]
        ++ lib.optional (lockArtifact != null) (steps.downloadArtifact { artifactName = lockArtifact; })
        ++ [
          (steps.installNix { })
          (steps.cachix { })
          {
            name = "Run the gates (ruff, ruff-strict, ruff format, pyright, grpclib-transports)";
            timeout-minutes = caps.staticChecks;
            run = ''
              nix build --no-link --print-build-logs --keep-going \
                ".#check-lint" ".#check-lint-strict" ".#check-format" ".#check-types" \
                ".#check-grpclib-transports"
            '';
          }
        ];
      }
    );

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
    mkJob (
      lib.optionalAttrs (needs != [ ]) { inherit needs; }
      // {
        steps = [
          (steps.checkout {
            inherit ref;
            # The range below needs the commits themselves, and the default
            # checkout fetches one.
            fetchDepth = 0;
          })
          {
            name = "Check the Conventional Commits subject of each pushed commit";
            timeout-minutes = caps.commitSubjects;
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
      steps = [
        (steps.checkout { inherit ref; })
      ]
      ++ lib.optional (lockArtifact != null) (steps.downloadArtifact { artifactName = lockArtifact; })
      ++ [
        (steps.installNix { })
        (steps.cachix { })
        {
          name = "Build documentation";
          timeout-minutes = caps.docsBuild;
          run = "nix build .#nanopynix-docs --out-link result --print-build-logs --print-out-paths";
        }
        (steps.verifyClosure { name = "Verify docs closure"; })
        {
          name = "Prepare Pages artifact";
          timeout-minutes = caps.docsPrepare;
          run = ''
            mkdir -p public
            cp -r --no-preserve=mode,ownership result/. public/
          '';
        }
        {
          uses = "actions/upload-pages-artifact@main";
          timeout-minutes = caps.docsUpload;
          "with" = {
            path = "public";
          };
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
          timeout-minutes = caps.docsDeploy;
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
    withTimeout
    # A job that this file does not build still needs a derived cap, and its
    # bespoke steps still need one each. `on_schedule.nix` writes two such
    # jobs.
    caps
    mkJob
    regularVersionNames
    regularBackends
    tsanVersionNames
    ubsanVersionNames
    mkRegularTestJob
    mkTsanTestJob
    mkUbsanTestJob
    mkStaticChecksJob
    mkCommitSubjectJob
    mkDocsBuildJob
    mkDocsDeployJob
    ;

  # Why these expand statically here, and through a GHA matrix in
  # `on_schedule.nix`, for the same jobs.
  #
  # The names come from `nanopynixVersionNames`, which this file reads out of
  # the flake at *render* time. The scheduled workflow runs `nix flake update`
  # before it tests anything, so its version list is not knowable until the
  # run is under way -- a bumped nixpkgs can add or drop a Nix version, and a
  # statically rendered list would silently never test the new one. That is
  # the whole of the difference, and it is why the scheduled side computes the
  # list in a step and feeds it to `strategy.matrix`.
  #
  # The per-commit side cannot use a matrix in exchange: the `jobs` dispatch
  # input selects by exact job name, and a matrix collapses eight jobs into
  # one id with eight legs. Both mechanisms are load-bearing, so the rule is
  # that every *kind* of test job exists on both sides -- regular, tsan, ubsan
  # -- and only the expansion differs.
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

  mkStaticUbsanTestJobs =
    {
      ref ? null,
      lockArtifact ? null,
      needs ? [ ],
    }:
    builtins.listToAttrs (
      map (version: {
        name = "test-ubsan-${lib.removeSuffix "-ubsan" version}";
        value = mkUbsanTestJob {
          inherit
            version
            ref
            lockArtifact
            needs
            ;
        };
      }) ubsanVersionNames
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
