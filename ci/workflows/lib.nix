# Shared GitHub Actions job builders.  This file is imported by the rendered
# workflow entrypoints; ci/render.py deliberately renders only on_*.nix.
#
# **No step here carries a script.** Every `run:` is one line, and it calls
# either one command or a package from `ci/steps.nix`. Every value that comes
# from a workflow expression reaches a step through `env:`. `CLAUDE.md` gives
# the rule and `tests/meta/test_ci_step_policy.py` keeps it.
{ }:
let
  # `import ../../.`, and not `builtins.getFlake`.
  #
  # This file used to read `flake.packages.<system>` and pick the test runners
  # out of it by a `passthru.addToMatrix` marker, because a flake output set is
  # flat and the version names had to survive being flattened into it. That is
  # no longer necessary: `nix build --file . <attrpath>` reaches any attribute,
  # so CI names `ciSteps.nix_2_34-tsan` directly and this file reads the same
  # attributes CI builds. The marker is gone with it.
  repo = import ../../. { };
  inherit (repo) lib ciVersionMatrix;

  ghalib = import ../../ghanix { inherit lib; };
  inherit (ghalib)
    steps
    withCond
    withTimeout
    evalWorkflow
    ;

  # `default.nix` groups the version names, so this file no longer repeats the
  # variant suffixes as a second list, and `on_schedule.nix` no longer writes
  # them a third time as a regular expression.
  inherit (ciVersionMatrix)
    regular
    tsan
    ubsan
    asan
    nogc
    ;

  # The `outputs` of the `update-lockfile` job, one for each group above.
  versionMatrixOutputs = builtins.listToAttrs (
    map (group: {
      name = "${group}_versions";
      value = "\${{ steps.versions.outputs.${group}_versions }}";
    }) (builtins.attrNames ciVersionMatrix)
  );

  # Every gate of nix/checks.nix, so a new one cannot be forgotten by the job
  # that is supposed to run them all.
  #
  # `isDerivation` is load-bearing: `checks` comes from `callPackage`, so
  # `makeOverridable` adds `override` and `overrideDerivation` to it, and both
  # are functions that `nix build` cannot realise.
  checkAttrs = builtins.attrNames (lib.filterAttrs (_: lib.isDerivation) repo.checks);

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
    # `nix build` of the CI step package, which pulls the test runner into its
    # closure. cachix holds the closure, so this is a fetch and a build of the
    # five packages of this repository: 2.3 minutes. The cap holds a cold build
    # of Nix itself, which a bumped nixpkgs causes.
    build = 30;
    # The same build, instrumented. Cold, and measured: 25 minutes for TSAN,
    # 38 for UBSan on the slowest version. A change to nix/sanitizer.nix is
    # the only thing that makes it cold.
    tsanBuild = 45;
    ubsanBuild = 60;
    # The ASAN build has two numbers now, and they answer a question this
    # comment used to guess at: 26 minutes cold in run 30860160011, and 25
    # minutes in run 30883251498, which builds boost as well. `sanitizeBoost`
    # in nix/sanitizer.nix gives the reason that variant needs its own boost,
    # and the second number says that boost costs nothing next to the nix
    # closure. The cap stays at the UBSan one.
    # There is no `nogcBuild`: `-Dgc=disabled` rebuilds nix-expr, nix-flake
    # and the bindings and nothing else -- measured, three derivations -- so
    # that job takes the plain `build` cap.
    asanBuild = 60;
    # The full suite. It takes 8 to 13 minutes on every version and backend,
    # and 9.7 to 13 under UBSan (run 30782379867). The no-collector job takes
    # the same cap, and it measured 12 minutes locally: a fork for each
    # in-process test costs about half again as much as one process.
    suite = 30;
    # `tests/nanopynix` under ASAN. Three runs measured the way here:
    #
    #   30860160011  686 tests, stopped at a 60-minute cap
    #   30883251498  686 tests, stopped at a 60-minute cap
    #   30895974566  806 tests, stopped at a 120-minute cap, 0 ASAN reports
    #
    # The third raised the three deadlines and got *slower* per test, because
    # each failure then waited out a longer clock. `ci/steps.nix` answers both
    # halves: it drops `pynix/tests`, where all ten of that run's failures
    # were, and brings the deadlines back to a middle ground. The remaining
    # selection is about 600 tests, and the passing rate of run 30895974566
    # puts that inside 60 minutes. This is the measurement that confirms it.
    asanSuite = 60;
    # One run of the concurrency soak, in the jobs that are not TSAN. It took
    # 45 seconds in the TSAN jobs of run 30930842932, which is the slow build.
    soak = 15;
    # Five runs of the concurrency soak, one per seed. Each run deals every
    # eligible test into eight overlapping lanes -- see nanopynix_testing.soak.
    tsanStress = 25;
    tsanBroad = 20;
    # `nix build` of every gate, which takes about a minute between them.
    staticChecks = 20;
    # One `git rev-list` and one `grep` for each commit pushed, plus the
    # evaluation that builds the script. It was 5 while the body lived in the
    # workflow file and needed no Nix at all.
    commitSubjects = 15;
    # Three `sysctl` calls, behind one evaluation.
    sandbox = 10;
    # The documentation build, and the copy of its output into `public/`.
    docsBuild = 30;
    docsPrepare = 10;
    # An upload of the whole site, and then a wait on GitHub Pages. Both are
    # out of our hands.
    docsUpload = 15;
    docsDeploy = 20;
    # `actions/deploy-pages` polls the Pages API, and it gives up on its own
    # after `timeout` milliseconds. That limit defaults to ten minutes, which
    # is below `docsDeploy` above, so the action decided the deadline and the
    # job cap never applied. Measured, on two runs of the same site:
    #
    #     2026-08-06 11:12:23 -> 11:21:21   success, 8m58s
    #     2026-08-06 14:52:37 -> 15:02:47   "Timeout reached, aborting!"
    #
    # A deployment of this site takes about nine minutes, so the default left
    # one minute of margin. This raises the limit of the action, and keeps it
    # under `docsDeploy` so that the job cap stays the outer bound.
    docsDeployPollMs = 15 * 60 * 1000;
    # An upload to Codecov of one XML file.
    codecov = 10;
    # `nix flake update`, which fetches every input.
    flakeUpdate = 20;
    # One `echo` for each group, behind the evaluation of the updated flake.
    versionMatrix = 20;
    # One commit and one push, by an action.
    autoCommit = 10;
    # **The whole wheel closure, from cold.** This build is not the ordinary
    # `nix build` of a Nix version: `nix/cxx-stdenv.nix` gives the closure its
    # own compiler wrapper, so cachix holds none of it until this job has run
    # once, and boost, openssl and Nix itself all compile here.
    #
    # **Measured on the runner, from cold.** Run 31605397637 is the first run
    # of this job, so cachix held none of the closure. The build compiled 95
    # derivations on `ubuntu-24.04` in 40 minutes, and 96 on `ubuntu-24.04-arm`
    # in 34 minutes. The rest of each closure came from `cache.nixos.org`: 296
    # paths and 229 paths.
    #
    # 120 gives three times the measurement. The number that a cap must beat is
    # the build that hangs, and not the build that is slow: a step that stops
    # after 2 hours reports the hang, and a step that stops after 4 hours
    # reports the same hang and spends twice as much of the runner.
    #
    # **360 minutes is the hard limit of a job, and `mkJob` derives the job cap
    # from this number.** The other steps of the job and `jobSlack` add 95, so
    # a build cap above 265 gives a job cap that GitHub never applies: it stops
    # the job at its own limit instead, and the run reports a cancellation
    # rather than the step that ran out of time.
    #
    # **A bump of nixpkgs is the case that can exceed this.** The 296 paths
    # above are substitutions, and a flake update can land on a revision that
    # `cache.nixos.org` has not built yet. The step then compiles them as well.
    # Raise this number when that happens, and record the run that measured it.
    wheelBuild = 120;
    # `scripts/wheel-inspect.sh` unzips the wheel and reads each object with
    # `readelf`. It takes seconds.
    wheelInspect = 10;
    # `scripts/wheel-smoke.sh` pulls a Rocky Linux 9 image, installs an
    # interpreter with `uv`, and evaluates. The pull and the interpreter are
    # the whole of it.
    wheelSmoke = 20;
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
  # entirely (see nanopynix_testing.nix_environment), so the CI runner's own
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

  # `nix run --file .`, for a step that needs no out-link. The attribute path
  # is the whole interface, and `ci/steps.nix` holds the body.
  mkNixRunStep =
    {
      name,
      attr,
      cap,
      env ? null,
    }:
    {
      inherit name;
      timeout-minutes = cap;
      run = "nix run --file . ciSteps.${attr}";
    }
    // lib.optionalAttrs (env != null) { inherit env; };

  # The runner has to allow an unprivileged user namespace before any test
  # that unshares one can run.
  mkSandboxStep =
    { }:
    mkNixRunStep {
      name = "Enable Nix sandbox namespaces";
      attr = "enable-sandbox-namespaces";
      cap = caps.sandbox;
    };

  # Build the CI step package of one version, and leave it at `result`.
  #
  # `$CI_STEP` is a job-level environment variable, so the version reaches this
  # command without a workflow expression in the body -- which is what keeps
  # the scheduled workflow, where the version is `${{ matrix.version }}`, on
  # the same one line as the per-commit workflow.
  mkBuildStep =
    { name, cap }:
    {
      inherit name;
      timeout-minutes = cap;
      run = ''nix build --file . "$CI_STEP" --out-link result --print-build-logs --print-out-paths'';
    };

  # Run one subcommand of the package that `mkBuildStep` left at `result`.
  mkRunStep =
    {
      name,
      subcommand,
      cap,
    }:
    {
      inherit name;
      timeout-minutes = cap;
      run = "./result/bin/nanopynix-ci ${subcommand}";
    };

  # The concurrency soak, as a step of its own. `ci/steps.nix` says why it
  # must not share a process with the rest of the suite.
  mkSoakStep =
    { label }:
    mkRunStep {
      name = "Run the concurrency soak (${label})";
      subcommand = "soak";
      cap = caps.soak;
    };

  # The job-level environment of a test job: which step package to build, and
  # for a regular build, which backend to drive.
  testJobEnv =
    {
      version,
      backend ? null,
    }:
    {
      CI_STEP = "ciSteps.${version}";
    }
    // lib.optionalAttrs (backend != null) { BACKEND = backend; };

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
        env = testJobEnv { inherit version backend; };
        steps = mkTestSetup { inherit ref lockArtifact; } ++ [
          (mkBuildStep {
            name = "Build the CI step package for Nix ${version}";
            cap = caps.build;
          })
          (steps.verifyClosure { name = "Verify test runner closure after build"; })
          (mkSandboxStep { })
          (mkRunStep {
            name = "Test nanopynix against Nix ${version} (full suite, ${backend} backend)";
            subcommand = "suite";
            cap = caps.suite;
          })
          (mkSoakStep { label = "${version}, ${backend} backend"; })
          (steps.uploadArtifact {
            name = "Upload test output";
            artifactName = "test-output-${backend}-${version}";
            path = "\${{ github.workspace }}/test-gdb-output.log";
          })
          # Uploaded on every run, not just a crash. The suite step inlines
          # this into the log only when pytest died of a signal, which turned
          # out to be the wrong trigger: the same suspected evaluator-state
          # corruption also surfaces as an ordinary *test failure* (a value of
          # the wrong type reaching nixpkgs' `env` type check, exit 1), and
          # that path needs the same registration history to correlate
          # against. As an artifact it costs no log space.
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

  # The concurrency soak and the concurrency tests, under ThreadSanitizer.
  # `ci/steps.nix` carries the collector reasoning, the seed loop and the
  # forensics that issue #69 asks for.
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
        env = testJobEnv { inherit version; };
        steps = mkTestSetup { inherit ref lockArtifact; } ++ [
          (mkBuildStep {
            name = "Build the TSAN CI step package (${bareVersion})";
            cap = caps.tsanBuild;
          })
          (mkSandboxStep { })
          (mkRunStep {
            name = "Run TSAN-instrumented concurrency soak (five seeds, local+daemon backends)";
            subcommand = "soak-seeds";
            cap = caps.tsanStress;
          })
          (mkRunStep {
            name = "Run TSAN-instrumented concurrency tests (single pass, local+daemon backends)";
            subcommand = "broad";
            cap = caps.tsanBroad;
          })
          (steps.uploadArtifact {
            name = "Upload TSAN output (${bareVersion})";
            artifactName = "tsan-race-report-${bareVersion}";
            # The soak manifests travel with the log. The log says which tests
            # were in flight when a race fired; a manifest replays that exact
            # composition with `--soak-manifest`.
            path = "\${{ github.workspace }}/tsan-output-${bareVersion}.log\n\${{ github.workspace }}/tsan-output-broad-${bareVersion}.log\n\${{ github.workspace }}/soak-${bareVersion}-run*.json\n";
          })
        ];
      }
    );

  # The full suite under UndefinedBehaviorSanitizer, against one Nix version.
  #
  # UBSan runs on its own rather than beside TSAN, although the two combine.
  # One sanitizer for each job keeps a red job attributable to one sanitizer.
  # The reason used to be 2.31, which the TSAN matrix skipped, and issue #126
  # dropped that version. See nix/sanitizer.nix.
  #
  # The AddressSanitizer job is `mkAsanTestJob` below, and it needs a libexpr
  # with no collector. `mkNoGCTestJob` is that build without the sanitizer.
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
        env = testJobEnv { inherit version; };
        steps = mkTestSetup { inherit ref lockArtifact; } ++ [
          (mkBuildStep {
            name = "Build the UBSAN CI step package (${bareVersion})";
            cap = caps.ubsanBuild;
          })
          (mkSandboxStep { })
          (mkRunStep {
            name = "Run UBSAN-instrumented suite (${bareVersion}, local backend)";
            subcommand = "suite";
            cap = caps.suite;
          })
          (mkSoakStep { label = "UBSAN, ${bareVersion}"; })
          (steps.uploadArtifact {
            name = "Upload UBSAN output (${bareVersion})";
            artifactName = "ubsan-report-${bareVersion}";
            path = "\${{ github.workspace }}/ubsan-output-${bareVersion}.log";
          })
        ];
      }
    );

  # The suite against a libexpr with no collector, and no sanitizer.
  #
  # **This job exists to keep a failure attributable.** The ASAN job below
  # runs against the same libexpr, so a red ASAN job has two possible causes:
  # a memory error, or an evaluator that does not work without the collector.
  # This one costs a plain build and a plain suite, and it tells the two
  # apart. It is also the answer to acceptance criterion 2 of issue #47, which
  # asks that an RPC worker run the suite against such a build.
  #
  # **Every test that builds an evaluator in the pytest process runs in a fork
  # of it, and that is what makes this job possible.** Such a build leaks by
  # design, and Nix's own package option gives the condition that makes the
  # leak acceptable: evaluation takes place within short-lived processes. An
  # RPC worker is one. A forked child is another, and it gives its memory back
  # when the test ends, so the pytest process stops accumulating.
  #
  # The measurements against nix_2_34-nogc say how much that is worth. The
  # same in-process subset needs about 10 GB in one process and 297 MB forked,
  # and it runs in 16 seconds rather than 86. The whole suite forked: 2077
  # passed, 13 skipped, 3 GB peak, 12 minutes.
  #
  # `nanopynix_testing.nix_runtime` holds the rule, and
  # `tests/meta/test_no_collector_rule.py` keeps it from going stale. Four
  # tests skip separately, through the `boehm_gc` capability that `build_info`
  # publishes: three measure the collector, and one abandons an evaluator in
  # the middle of work that Nix will not stop, which no fork can bound.
  mkNoGCTestJob =
    {
      version,
      ref ? null,
      lockArtifact ? null,
      needs ? [ ],
    }:
    let
      bareVersion = lib.removeSuffix "-nogc" version;
    in
    mkJob (
      lib.optionalAttrs (needs != [ ]) { inherit needs; }
      // {
        env = testJobEnv { inherit version; };
        steps = mkTestSetup { inherit ref lockArtifact; } ++ [
          (mkBuildStep {
            name = "Build the no-collector CI step package (${bareVersion})";
            # The plain cap, not a sanitizer one. `-Dgc=disabled` rebuilds
            # nix-expr, nix-flake and the bindings, and leaves nix-util,
            # nix-store and nix-fetchers alone -- measured, three derivations.
            cap = caps.build;
          })
          (mkSandboxStep { })
          (mkRunStep {
            name = "Run the suite without a collector (${bareVersion}, local backend)";
            subcommand = "suite";
            cap = caps.suite;
          })
          (mkSoakStep { label = "no collector, ${bareVersion}"; })
        ];
      }
    );

  # The suite under AddressSanitizer, against one Nix version. `mkNoGCTestJob`
  # above says how a build with no collector runs the whole suite, and this job
  # inherits that rule because it builds against the same libexpr: every test
  # that builds an evaluator in the pytest process runs in a fork of it.
  #
  # **The build has no collector, and that is not a tuning choice.** libexpr
  # refuses ASAN together with the Boehm collector, because a conservative
  # collector cannot see through the allocator of ASAN and will free an object
  # that is still live. The first ASAN variant of this repository passed the
  # flag through `NIX_CFLAGS_COMPILE`, which meson never reads, and it then
  # reported exactly that -- a tag read of a freed `Value`, which was not
  # evidence of anything. See `requiresNoGC` in nix/sanitizer.nix.
  #
  # `detect_leaks=0` is not a workaround. Without the collector the evaluator
  # allocates and never releases, so a leak checker reports the design of the
  # build. This job is here for a memory error.
  #
  # `detect_stack_use_after_return=1` is not the default, and it is the shape
  # of the defect of issue #34 -- a stack-local `nix::fetchers::Settings` whose
  # address outlived the frame. That defect is the acceptance test of issue
  # #35: restore it, and this job must go red.
  #
  # `PYTHONMALLOC=malloc` gives ASAN the allocations of CPython itself. Without
  # it a report names the arena rather than the caller.
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
    mkJob (
      lib.optionalAttrs (needs != [ ]) { inherit needs; }
      // {
        env = testJobEnv { inherit version; };
        steps = mkTestSetup { inherit ref lockArtifact; } ++ [
          (mkBuildStep {
            name = "Build the ASAN CI step package (${bareVersion})";
            cap = caps.asanBuild;
          })
          (mkSandboxStep { })
          (mkRunStep {
            name = "Run ASAN-instrumented suite (${bareVersion}, local backend)";
            subcommand = "suite";
            cap = caps.asanSuite;
          })
          (mkSoakStep { label = "ASAN, ${bareVersion}"; })
          (steps.uploadArtifact {
            name = "Upload ASAN output (${bareVersion})";
            artifactName = "asan-report-${bareVersion}";
            path = "\${{ github.workspace }}/asan-output-${bareVersion}.log";
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
  # **The list comes from `repo.checks`, so a new gate joins this job by
  # existing.** It was written by hand until this file could read the
  # attribute set directly, and a gate that nobody added here ran nowhere.
  #
  # `grpclib-transports` and `pytest-agent` are the odd ones out, each being a
  # run rather than a static tool. Both are here rather than in the `test-*`
  # matrix because both are version-independent: that matrix exists to run one
  # suite against each supported Nix version, and neither subproject links Nix
  # at all. Three copies of either would be three identical runs.
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
            name = "Run the gates (${builtins.concatStringsSep ", " checkAttrs})";
            timeout-minutes = caps.staticChecks;
            run = "nix build --file . --no-link --print-build-logs --keep-going ${
              builtins.concatStringsSep " " (map (attr: "checks.${attr}") checkAttrs)
            }";
          }
        ];
      }
    );

  # The wheel, built and then read. Issue #120 asks for this job, and it gives
  # the four defects that reached a finished build while no gate read any of
  # `nix/cxx-stdenv.nix`, `nix/cxx-runtime.nix`, `nix/lower-glibc.py`,
  # `nix/closure.nix` or `nix/nix-closure.nix`.
  #
  # **`auditwheel` is the floor gate, and it runs inside the build.**
  # `nix/wheel.nix` repairs to `manylinux_2_34` and `auditwheel` refuses a tag
  # that the objects do not support, so a raised floor fails `nix build` and
  # never reaches the steps below. Measured twice on 2026-08-12, both times as
  # "cannot repair ... because of the presence of too-recent versioned
  # symbols". So this job needs no floor check of its own, and the two steps
  # below answer what the build cannot.
  #
  # **One job for each architecture, and never emulation.** `runs-on` carries
  # the difference and nothing else does: `nix/cxx-stdenv.nix` reads
  # `stdenv.hostPlatform`, so an arm64 runner builds the aarch64 wheel with no
  # cross configuration. An x86-64 runner with binfmt would build the same
  # wheel under qemu, and it would take many hours.
  mkWheelJob =
    {
      runner ? "ubuntu-24.04",
      # `scripts/wheel-smoke.sh` runs the wheel, so it needs an interpreter of
      # the wheel's own architecture. That is what the native runner gives.
      smoke ? true,
      ref ? null,
      lockArtifact ? null,
      needs ? [ ],
    }:
    mkJob (
      lib.optionalAttrs (needs != [ ]) { inherit needs; }
      // {
        runs-on = runner;
        # `wheel-smoke.sh` takes the container runtime from here. It defaults to
        # podman, and a GitHub runner carries docker.
        env.WHEEL_SMOKE_RUNTIME = "docker";
        steps = [
          (steps.checkout { inherit ref; })
        ]
        ++ lib.optional (lockArtifact != null) (steps.downloadArtifact { artifactName = lockArtifact; })
        ++ [
          (steps.installNix { })
          (steps.cachix { })
          {
            name = "Build the wheel";
            timeout-minutes = caps.wheelBuild;
            run = "nix build --file . nanopynixWheel --out-link result-wheel --print-build-logs";
          }
          {
            # Reads the files and runs nothing, so it answers for either
            # architecture. It fails when an object asks the host for a C++
            # standard library.
            name = "Read the wheel";
            timeout-minutes = caps.wheelInspect;
            run = "./scripts/wheel-inspect.sh result-wheel";
          }
        ]
        ++ lib.optional smoke {
          # The check that reads nothing and runs everything: it installs the
          # wheel on a distribution whose glibc is the floor exactly, and
          # evaluates with it.
          name = "Load the wheel on Rocky Linux 9";
          timeout-minutes = caps.wheelSmoke;
          run = "./scripts/wheel-smoke.sh result-wheel";
        };
      }
    );

  # The one part of the commit convention that a machine can check.
  # `ci/steps.nix` carries the rule, and the two parts it deliberately leaves
  # alone.
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
            # The range needs the commits themselves, and the default checkout
            # fetches one.
            fetchDepth = 0;
          })
          (steps.installNix { })
          (mkNixRunStep {
            name = "Check the Conventional Commits subject of each pushed commit";
            attr = "commit-subjects";
            cap = caps.commitSubjects;
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
          run = "nix build --file . nanopynix-docs --out-link result --print-build-logs --print-out-paths";
        }
        (steps.verifyClosure { name = "Verify docs closure"; })
        (mkNixRunStep {
          name = "Prepare Pages artifact";
          attr = "prepare-pages";
          cap = caps.docsPrepare;
        })
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
          "with" = {
            timeout = caps.docsDeployPollMs;
          };
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
    mkNixRunStep
    mkSandboxStep
    regularBackends
    versionMatrixOutputs
    mkRegularTestJob
    mkTsanTestJob
    mkUbsanTestJob
    mkAsanTestJob
    mkNoGCTestJob
    mkStaticChecksJob
    mkWheelJob
    mkCommitSubjectJob
    mkDocsBuildJob
    mkDocsDeployJob
    ;

  # **Every workflow sets this, and every workflow needs it.**
  # `nix/compat.nix` normally overrides `self` with the local checkout, which
  # is right on a laptop and wrong on a runner: CI must evaluate the tree the
  # way the flake evaluator would, from the lockfile and through a store copy.
  # With this set, `nix build --file . <attrpath>` and `nix build .#<name>`
  # agree, which is what lets every step name a plain attribute path.
  workflowEnv = {
    FLAKE_COMPATISH_DISABLE_OVERRIDES = "1";
  };

  # Why these expand statically here, and through a GHA matrix in
  # `on_schedule.nix`, for the same jobs.
  #
  # The names come from `ciVersionMatrix`, which `default.nix` computes and
  # this file reads at *render* time. The scheduled workflow runs `nix flake
  # update` before it tests anything, so its version list is not knowable
  # until the run is under way -- a bumped nixpkgs can add or drop a Nix
  # version, and a statically rendered list would silently never test the new
  # one. That is the whole of the difference, and it is why the scheduled side
  # computes the list in a step and feeds it to `strategy.matrix`.
  #
  # The per-commit side cannot use a matrix in exchange: the `jobs` dispatch
  # input selects by exact job name, and a matrix collapses eight jobs into
  # one id with eight legs. Both mechanisms are load-bearing, so the rule is
  # that every *kind* of test job exists on both sides, and only the expansion
  # differs.
  #
  # **`asan` is on both sides now, and it has the number issue #35 asked
  # for.** That issue said "Do not put it in the per-commit workflow until the
  # run time is known". Run 30905635880 knows it: 1622 tests in 18m30s, and
  # 19m42s for the whole job. It earns the slot rather than merely fitting it,
  # because it is the only build that reports a use-after-free or a
  # stack-use-after-return -- which is how the defect of #34 was caught, as a
  # `stack-use-after-return` in `warnDirty`.
  #
  # **`nogc` is on both sides, and it has the number the rule asks for.** The
  # whole suite against nix_2_34-nogc measured 12 minutes at a 3 GB peak, so
  # it takes `caps.suite` with room to spare. It earns the per-commit slot
  # rather than merely fitting it: it is the only build where an evaluator
  # cannot hide a leak behind the collector, and where a worker that aborts
  # has nothing to reclaim its memory. Issue #55 exists because such an abort
  # happened inside one of these runs and reported success.
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
        }) regular
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
      }) ubsan
    );

  mkStaticNoGCTestJobs =
    {
      ref ? null,
      lockArtifact ? null,
      needs ? [ ],
    }:
    builtins.listToAttrs (
      map (version: {
        name = "test-nogc-${lib.removeSuffix "-nogc" version}";
        value = mkNoGCTestJob {
          inherit
            version
            ref
            lockArtifact
            needs
            ;
        };
      }) nogc
    );

  mkStaticAsanTestJobs =
    {
      ref ? null,
      lockArtifact ? null,
      needs ? [ ],
    }:
    builtins.listToAttrs (
      map (version: {
        name = "test-asan-${lib.removeSuffix "-asan" version}";
        value = mkAsanTestJob {
          inherit
            version
            ref
            lockArtifact
            needs
            ;
        };
      }) asan
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
      }) tsan
    );
}
