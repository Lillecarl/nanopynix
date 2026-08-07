# Every body that a GitHub Actions step used to carry inline.
#
# **A step body is a package here, and not a `run:` string in a YAML file.**
# Four things make that the better place for it:
#
# 1. **A gate reads it.** `writeShellApplication` runs shellcheck over what it
#    builds. `check-shell` covers `scripts/*.sh` and nothing else, so a body
#    inside `ci/workflows/lib.nix` was read by no tool at all.
# 2. **It does not depend on the runner image.** `runtimeInputs` names `jq`,
#    `git` and `unshare`, so the same step runs on a laptop, on a GitHub
#    runner, or on another CI service.
# 3. **It runs locally, against one Nix version.** That is the whole reason
#    these live beside `tests`, and CI runs the identical command:
#
#        nix build --file . ciSteps.nix_2_34 --out-link result
#        BACKEND=local ./result/bin/nanopynix-ci soak
#
# 4. **Changing it costs no re-render.** A `run:` body reaches CI only through
#    the 3347 lines of generated YAML, and the shell quoting and the GitHub
#    expression syntax are interleaved there.
#
# `tests/meta/test_ci_step_policy.py` keeps the rule: a rendered `run:` is one
# line, and a `${{ ... }}` expression reaches a step through `env:` only.
#
# Each script reads `$GITHUB_WORKSPACE` for the directory that artifacts go to,
# and falls back to the working directory when that variable is absent, which
# is what makes a local run work.
{
  pkgs,
  tests,
  ciVersionMatrix,
  variantSuffixes,
}:
let
  inherit (pkgs) lib;

  # The arguments that every invocation of the runner shares. The runner has no
  # pytest-agent, so `-rsxXfE` and a short traceback are the only detail that a
  # failure leaves behind. `ci/experiments.nix` says the same thing.
  baseArgs = [
    "--verbose"
    "--tb=short"
    "-rsxXfE"
    "--run-temp-store-builds"
  ];

  # A sanitized run adds `--capture=no`, because a report that the sanitizer
  # writes to stderr belongs in the log even when the test that provoked it
  # passes.
  uncapturedArgs = baseArgs ++ [ "--capture=no" ];

  quote = lib.escapeShellArgs;

  # `nix` itself is deliberately absent from every `runtimeInputs` below.
  # `writeShellApplication` prepends its inputs and keeps the ambient PATH, so
  # `nix` resolves to the installation that owns the store the tests just wrote
  # to. A pinned copy from this closure would talk to the same store, but it
  # would also be a second Nix on a machine that already has the right one.
  mkStep =
    {
      name,
      runtimeInputs ? [ ],
      text,
    }:
    pkgs.writeShellApplication {
      inherit name runtimeInputs;
      text = ''
        # A local run has no workspace, and every artifact path below is
        # relative to one.
        : "''${GITHUB_WORKSPACE:=$PWD}"

      ''
      + text;
    };

  # ---------------------------------------------------------------------------
  # The per-version steps
  # ---------------------------------------------------------------------------

  # `nanopynix-tests-nix_2_34-tsan` -> "nix_2_34-tsan".
  versionNames = map (lib.removePrefix "nanopynix-tests-") (builtins.attrNames tests);

  suffixOf = version: lib.findFirst (suffix: lib.hasSuffix suffix version) "" variantSuffixes;

  # "nix_2_34-tsan" -> "tsan", and "nix_2_34" -> "regular".
  kindOf =
    version:
    let
      suffix = suffixOf version;
    in
    if suffix == "" then "regular" else lib.removePrefix "-" suffix;

  # "nix_2_34-tsan" -> "nix_2_34". Every log and artifact name uses the bare
  # version, because the variant is already in the job name.
  bareOf = version: lib.removeSuffix (suffixOf version) version;

  runnerOf = version: lib.getExe' tests."nanopynix-tests-${version}" "nanopynix-tests";

  # The soak, which every kind runs and which no kind runs in the same way.
  #
  # **The soak must not share a process with the rest of the suite.** It drives
  # eight overlapping lanes of existing tests through one interpreter, and it
  # reaches the corruption of issue #70, which ends the process with SIGSEGV.
  # Inside the full-suite invocation that one crash took the results of about
  # 1700 other tests with it -- measured in run 30931403310, job
  # `test-daemon-nix_2_34`. Every full-suite invocation therefore passes
  # `-m "not soak"`, and this is where the soak runs instead.
  #
  # It still fails its job. The soak reports a real defect, and a job that goes
  # green over one is worth nothing. What the separation changes is the blast
  # radius, and not the verdict.
  soakBody =
    { runner, env }:
    ''
      soak() {
        env ${quote env} \
          ${runner} ${quote uncapturedArgs} \
          --nix-test-backends "$BACKEND" -m soak
      }
    '';

  # The shape that the regular, UBSAN, ASAN and no-collector suites share:
  # run the suite, keep the whole log, and then read that log for a report.
  #
  # **The log decides, and not the exit status alone.** `halt_on_error=1` makes
  # a sanitizer kill the process that reports, but a report raised inside a
  # forkserver worker can still be reaped into a plain test failure. A kind
  # with no report signature passes an empty pattern and keeps its status.
  scanBody = ''
    # $1 log file, $2 extended-regex report signature ("" for none), rest the
    # command. Returns the status of the command, or 1 when the log holds a
    # report.
    run_and_scan() {
      local logfile="$1"
      local pattern="$2"
      shift 2
      local status=0
      "$@" 2>&1 | tee -a "$logfile" || status=$?
      if [ -n "$pattern" ] && grep -qE "$pattern" "$logfile"; then
        echo "::error::sanitizer report -- see the log above"
        return 1
      fi
      return "$status"
    }
  '';

  # The full suite with coverage, on a plain build. The only kind that collects
  # coverage, and the only one that deletes the store paths its builds left.
  regularSuite =
    { runner }:
    ''
      suite() {
        local paths_to_delete="$GITHUB_WORKSPACE/nanopynix-test-store-paths.txt"
        # Not stderr: pytest captures fd 2 per test by default and only prints
        # the buffer when that test fails, so a SIGSEGV takes the collector
        # thread log down with it -- a full run that crashed produced zero
        # diagnostic lines. A file is outside pytest's capture.
        local gc_thread_log="$GITHUB_WORKSPACE/gc-thread-debug.log"
        rm -f "$paths_to_delete" "$gc_thread_log"

        local status=0
        # NANOPYNIX_COVERAGE rather than pytest-cov's --cov: the runner then
        # measures with `coverage run` and combines after pytest exits.
        # pytest-cov combines *inside* the run, alongside live evaluator
        # threads and forkserver workers, and that combine intermittently
        # failed with "database is locked" -- exit 3 on a job whose tests had
        # all passed. See nanopynix/tests.nix.
        env NANOPYNIX_CORE_DEBUG=1 \
            NANOPYNIX_GC_THREAD_DEBUG=1 \
            NANOPYNIX_GC_THREAD_DEBUG_FILE="$gc_thread_log" \
            NANOPYNIX_RPC_TIMEOUT=30 \
            PYTHONDONTWRITEBYTECODE=1 \
            COVERAGE_FILE="$GITHUB_WORKSPACE/.coverage" \
            NANOPYNIX_COVERAGE=1 \
            NANOPYNIX_COVERAGE_XML="$GITHUB_WORKSPACE/coverage.xml" \
            NANOPYNIX_TEST_DELETE_PATHS_FILE="$paths_to_delete" \
          ${runner} ${quote baseArgs} \
          --nix-test-backends "$BACKEND" \
          -m "not soak" \
          --junitxml="$GITHUB_WORKSPACE/junit.xml" \
          2>&1 | tee "$GITHUB_WORKSPACE/test-gdb-output.log" || status=$?

        # Only on a crash: this file has one line per evaluator thread
        # registration and is long, so it is worth the log space solely when
        # there is a faulting LWP to correlate it against.
        if [ "$status" -gt 128 ] && [ -s "$gc_thread_log" ]; then
          {
            echo "=== Boehm GC thread registration log ($(wc -l <"$gc_thread_log") lines) ==="
            cat "$gc_thread_log"
          } >> "$GITHUB_WORKSPACE/test-gdb-output.log"
        fi

        if [ -s "$paths_to_delete" ]; then
          nix store delete --stdin < "$paths_to_delete" || true
        fi
        return "$status"
      }
    '';

  # The five-seed TSAN soak.
  #
  # **There is no retry, and no abort budget.** Both existed while issue #69
  # was open, when the collector aborted often and for no known reason. The
  # cause is known now, and it is corrected: `99f74d82` registers the thread
  # that builds an evaluator with Boehm, and #72 gives the mechanism -- the
  # collector signalled a `pthread_t` that glibc had already handed on. Every
  # `test-tsan-*` job since that commit reports zero aborts: 18 jobs, six runs,
  # three Nix versions, five seeds against two backends each. A budget above
  # zero therefore tolerates nothing that happens, and hides a regression of
  # `99f74d82` up to five times over. The budget was already 0 before this
  # became a script, so the loop that carried it could never take a second
  # attempt.
  #
  # `GC_INITIAL_HEAP_SIZE` gives Boehm a heap it does not have to grow, and a
  # collection that never happens cannot fail to stop the world. Nix sets this
  # itself when the variable is absent, to 25% of RAM capped at 384 MiB
  # (libexpr/eval-gc.cc), and the soak exhausts that: it holds 66 tests and
  # their evaluators in one process. The cap is what this raises, and
  # `GC_expand_hp` takes virtual rather than resident memory, so the runner
  # pays little for it. **This lowers the abort rate, and corrects nothing.**
  #
  # `GC_PRINT_STATS` turns on `GC_COND_LOG_PRINTF`, and `resend_lost_signals`
  # uses it to report how many threads still owe an acknowledgement on each
  # pass. That count is the first thing issue #69 needs. `GC_LOG_FILE` keeps
  # the rest of the collector chatter out of the job log.
  tsanSoakSeeds =
    { runner, bare }:
    ''
      soak_seeds() {
        local logfile="$GITHUB_WORKSPACE/tsan-output-${bare}.log"
        local seed status runlog gclog

        for seed in $(seq 1 5); do
          echo "=== TSAN run $seed ===" | tee -a "$logfile"
          runlog="$(mktemp)"
          gclog="$(mktemp)"
          status=0
          unshare --user --map-root-user --mount --pid --fork --mount-proc \
            env NANOPYNIX_CORE_DEBUG=1 \
                NANOPYNIX_RPC_TIMEOUT=30 \
                NANOPYNIX_TEST_SANITIZER=tsan \
                PYTHONDONTWRITEBYTECODE=1 \
                GC_INITIAL_HEAP_SIZE=2147483648 \
                GC_PRINT_STATS=1 \
                GC_LOG_FILE="$gclog" \
              ${runner} ${quote uncapturedArgs} \
              --nix-test-backends local,daemon \
              -m soak --soak-seed="$seed" \
              --soak-report="$GITHUB_WORKSPACE/soak-${bare}-run$seed.json" \
              2>&1 | tee "$runlog" || status=$?
          cat "$runlog" >> "$logfile"
          echo "=== TSAN run $seed exit status: $status ===" | tee -a "$logfile"

          # Read the attempt, and not the whole log. The log holds every
          # earlier seed, so a match there says nothing about this one.
          if grep -q "ThreadSanitizer: data race" "$runlog"; then
            rm -f "$runlog" "$gclog"
            echo "::error::genuine ThreadSanitizer data race on run $seed -- see the log above"
            return 1
          fi

          if [ "$status" -ne 0 ]; then
            # The collector log goes out on any non-zero status, and not only
            # on an abort. A stop-the-world that fails can hang rather than
            # abort, and then the status is the status of pytest. Run
            # 31189073155 hit that: `test_soak_inproc[local]` reached its 120s
            # deadline with `nix-eval_0` inside `lock_flake`, the status was 1,
            # and these lines were deleted unread. See issue #99.
            echo "=== Boehm resend log of run $seed ===" | tee -a "$logfile"
            grep -E "Resent|Lost some threads|stop_world" "$gclog" \
              | tail -40 | tee -a "$logfile" || true
            if [ "$status" -eq 134 ] && grep -q "Signals delivery fails" "$runlog"; then
              echo "::error::the collector could not stop the world on run $seed -- see issue #69"
            else
              echo "::error::run $seed failed with status $status -- see the log above"
            fi
            rm -f "$runlog" "$gclog"
            return 1
          fi

          rm -f "$runlog" "$gclog"
        done
      }
    '';

  # One pass over the concurrency tests, under TSAN.
  #
  # **This reports on the log and not on the exit status, and that is a hole.**
  # A pass that ends for a reason none of these patterns match reports success.
  # A run stopped by its own deadline is the case that matters, because it is
  # the shape of issue #99. The behaviour is unchanged from the step this
  # replaces, deliberately: correcting it turns jobs red for a reason that has
  # nothing to do with moving the body into a script.
  tsanBroad =
    { runner, bare }:
    ''
      broad() {
        local logfile="$GITHUB_WORKSPACE/tsan-output-broad-${bare}.log"
        local status=0
        unshare --user --map-root-user --mount --pid --fork --mount-proc \
          env NANOPYNIX_CORE_DEBUG=1 \
              NANOPYNIX_RPC_TIMEOUT=30 \
              NANOPYNIX_TEST_SANITIZER=tsan \
              PYTHONDONTWRITEBYTECODE=1 \
            ${runner} ${quote uncapturedArgs} \
            --nix-test-backends local,daemon -m concurrency \
            2>&1 | tee -a "$logfile" || status=$?
        echo "=== TSAN broad pass exit status: $status ===" | tee -a "$logfile"
        if grep -q "ThreadSanitizer: data race" "$logfile"; then
          echo "::error::genuine ThreadSanitizer data race detected -- see the log above"
          return 1
        fi
        if grep -qE "[0-9]+ (failed|error)|Fatal Python error|pthread_kill failed at suspend" "$logfile"; then
          echo "::error::pytest reported a real failure, or the process crashed -- see the log above"
          return 1
        fi
      }
    '';

  # A sanitized or no-collector suite: one call of `run_and_scan`.
  #
  # `env`, `pattern` and `args` are what differ between the kinds.
  scannedSuite =
    {
      runner,
      bare,
      kind,
      env,
      pattern,
      args,
    }:
    ''
      suite() {
        run_and_scan "$GITHUB_WORKSPACE/${kind}-output-${bare}.log" ${quote [ pattern ]} \
          env ${quote env} \
          ${runner} ${quote args} \
          --nix-test-backends local -m "not soak"
      }
    '';

  # The environment of each kind, and the report signature that its log carries.
  #
  # UBSAN: "Unexpected condition" is the message of `nix::unreachable`, which
  # is what `nixUnreachableWhenHardened` becomes once `NIX_UBSAN_ENABLED` is
  # on. That path never prints the words "runtime error", so the first two
  # patterns would miss it.
  #
  # ASAN: three deadlines, and the default of each one is written for a build
  # with no instrumentation. Run 30891726124 measured what happens when they
  # stay: tests failed on a clock rather than on an assertion about Nix. Run
  # 30895974566 then measured the opposite -- 600/120/900 dropped throughput
  # from 15.0 tests a minute to 6.7, because each failure waited out a much
  # longer deadline. These are the middle ground. See issue #61.
  kindEnv = {
    regular = [
      "NANOPYNIX_CORE_DEBUG=1"
      "NANOPYNIX_RPC_TIMEOUT=30"
      "PYTHONDONTWRITEBYTECODE=1"
    ];
    ubsan = [
      "NANOPYNIX_CORE_DEBUG=1"
      "NANOPYNIX_RPC_TIMEOUT=60"
      "NANOPYNIX_TEST_SANITIZER=ubsan"
      "PYTHONDONTWRITEBYTECODE=1"
    ];
    asan = [
      "NANOPYNIX_CORE_DEBUG=1"
      "NANOPYNIX_RPC_TIMEOUT=180"
      "NANOPYNIX_SHUTDOWN_TIMEOUT=60"
      "NANOPYNIX_TEST_TIMEOUT=300"
      "NANOPYNIX_TEST_SANITIZER=asan"
      "PYTHONDONTWRITEBYTECODE=1"
    ];
    nogc = [
      "NANOPYNIX_CORE_DEBUG=1"
      "NANOPYNIX_RPC_TIMEOUT=30"
      "PYTHONDONTWRITEBYTECODE=1"
    ];
    tsan = [ ];
  };

  kindPattern = {
    ubsan = "(UndefinedBehaviorSanitizer|runtime error):|Unexpected condition in ";
    asan = "AddressSanitizer|LeakSanitizer";
    nogc = "";
    regular = "";
    tsan = "";
  };

  # **`tests/pynix` is out of the ASAN selection, and run 30895974566 is the
  # reason.** That run reached a 120-minute cap and reported 806 tests, 742
  # passed, 10 failed, 0 AddressSanitizer reports, killed inside
  # `tests/pynix/test_lsp.py`. All ten failures were in `tests/pynix`, and none
  # were in `tests/nanopynix`, so nothing is lost by the cut and the whole
  # failure set goes with it. Only instrumented code reports:
  # nanopynix-bindings, the Nix libraries and boost. `tests/nanopynix` drives
  # that surface directly, and `tests/pynix` reaches the bindings only along
  # paths `tests/nanopynix` already covers.
  #
  # **The no-collector suite keeps pytest's capture, and the sanitized ones do
  # not.** A sanitizer writes its report to stderr from inside the process, so
  # capture would hold that report in the buffer of whichever test was running
  # and print it only if that test failed. The no-collector build emits no such
  # report, so `--capture=no` buys it nothing.
  #
  # **It also costs almost nothing, and that number is here so that nobody
  # repeats the guess this comment first carried.** The move to a script gave
  # this job `--capture=no` by accident, and run 31196403276 measured the
  # result: the no-collector job wrote 4814 log lines uncaptured, against 6201
  # for the captured regular job beside it. So the reason to keep capture here
  # is fidelity and not cost -- this is what the job did before the move.
  #
  # Issue #55 is the argument for changing it: that job exists to catch a
  # worker that aborts and reports success, and uncaptured output makes a late
  # crash visible. Decide that on its own, and not inside a refactor.
  kindArgs = {
    ubsan = uncapturedArgs;
    asan = uncapturedArgs ++ [ "--ignore=tests/pynix" ];
    nogc = baseArgs;
  };

  # The subcommands of one version, as a `case` body plus the functions it
  # calls. Each variant gets only its own, so an unknown subcommand is an
  # error and not a silent no-op.
  subcommandsFor =
    version:
    let
      runner = runnerOf version;
      bare = bareOf version;
      kind = kindOf version;
      soakEnv = kindEnv.${kind};
    in
    if kind == "tsan" then
      {
        arms = {
          "soak-seeds" = "soak_seeds";
          broad = "broad";
        };
        body = tsanSoakSeeds { inherit runner bare; } + tsanBroad { inherit runner bare; };
      }
    else
      {
        arms = {
          suite = "suite";
          soak = "soak";
        };
        body =
          scanBody
          + (
            if kind == "regular" then
              regularSuite { inherit runner; }
            else
              scannedSuite {
                inherit runner bare kind;
                env = kindEnv.${kind};
                pattern = kindPattern.${kind};
                args = kindArgs.${kind};
              }
          )
          + soakBody {
            inherit runner;
            env = soakEnv;
          };
      };

  # Where the backend of a run comes from.
  #
  # A regular build takes it from the job, so that `test-local-*` and
  # `test-daemon-*` are two jobs over one script. Every sanitized kind fixes it
  # at `local`: a sanitized run costs too much to do twice, and the daemon
  # backend forks a handler process per connection -- a shape worth a separate
  # decision once the run time of the simple case is a number rather than a
  # guess. TSAN names both backends in the invocation itself, so it reads no
  # such variable and must not declare one.
  backendPreamble =
    version:
    let
      kind = kindOf version;
    in
    if kind == "tsan" then
      ""
    else if kind == "regular" then
      ''
        # The job decides, so that test-local-* and test-daemon-* are two jobs
        # over one script.
        : "''${BACKEND:?BACKEND must name a --nix-test-backends value, for example local}"
      ''
    else
      ''
        # Fixed for a sanitized build. See ci/steps.nix.
        BACKEND="''${BACKEND:-local}"
      '';

  mkVersionStep =
    version:
    let
      sub = subcommandsFor version;
      subcommands = builtins.attrNames sub.arms;
      dispatch = lib.concatMapStrings (
        subcommand: "  ${subcommand}) ${sub.arms.${subcommand}} ;;\n"
      ) subcommands;
    in
    mkStep {
      # The same binary name for every version, because the attribute path
      # already carries the version: `ciSteps.nix_2_34-tsan` builds to
      # `bin/nanopynix-ci`. Two derivations may share a name and differ by
      # hash, so nothing collides, and a step reads the same on every job.
      name = "nanopynix-ci";
      runtimeInputs = with pkgs; [
        coreutils
        gnugrep
        util-linux
      ];
      text = ''
        ${backendPreamble version}
        ${sub.body}
        case "''${1:-}" in
        ${dispatch}  *)
            echo "usage: ''${0##*/} {${builtins.concatStringsSep "|" subcommands}}" >&2
            echo "  the ${kindOf version} step package for ${version}" >&2
            exit 2
            ;;
        esac
      '';
    };

  # Keyed by the version name exactly as `nanopynixVersions` keys it, so
  # `ciSteps.nix_2_34-tsan` needs no translation from the scope it tests.
  versionSteps = lib.genAttrs versionNames mkVersionStep;

  # ---------------------------------------------------------------------------
  # The steps that no Nix version changes
  # ---------------------------------------------------------------------------

  sharedSteps = {
    # The one part of the commit convention that a machine can check: the
    # Conventional Commits prefix that CLAUDE.md requires. It checks the
    # *shape* and not a list of allowed types, because CLAUDE.md gives
    # `feat(scope):` and `fix(tests):` as examples and never agreed a taxonomy.
    # The shape alone catches the subjects that this repository actually
    # produced before the convention settled -- `fmt`, `ASD-STE100`, `add gdb
    # to devshell`.
    #
    # DELIBERATELY NOT CHECKED, because neither is machine-decidable:
    #
    #   `Closes #<number>` is conditional. A commit completes an issue, or it
    #   does not, and no machine knows which. A required trailer would train
    #   people to write `Closes` for partial work -- the exact failure that
    #   CLAUDE.md warns about, and worse than no check.
    #
    #   The `Co-Authored-By` and `Claude-Session` trailers are contextual. A
    #   commit that a person writes without an agent carries neither, and it
    #   must not fail for that.
    #
    # The push base comes from the event payload rather than from a workflow
    # expression, so this needs nothing but the environment GitHub already
    # sets. A local run gives `BASE_SHA` itself, or gets the head alone.
    commit-subjects = mkStep {
      name = "ci-commit-subjects";
      runtimeInputs = with pkgs; [
        coreutils
        git
        gnugrep
        jq
      ];
      text = ''
        head_sha="''${GITHUB_SHA:-HEAD}"
        base_sha="''${BASE_SHA:-}"
        if [ -z "$base_sha" ] && [ -n "''${GITHUB_EVENT_PATH:-}" ] && [ -r "''${GITHUB_EVENT_PATH:-}" ]; then
          base_sha="$(jq -r '.before // ""' "$GITHUB_EVENT_PATH")"
        fi

        # `before` is the all-zero sha for a new branch, and it names a commit
        # that the remote no longer has after a force push. The range means
        # nothing in either case, so check the head alone.
        if [ -n "$base_sha" ] && git cat-file -e "$base_sha^{commit}" 2>/dev/null; then
          commits="$(git rev-list --no-merges "$base_sha..$head_sha")"
        else
          echo "no usable base commit; checking $head_sha alone"
          commits="$(git rev-list --no-merges -1 "$head_sha")"
        fi

        # Say how many, so that a pass is auditable. A range with no commits
        # also exits 0, and a gate that quietly checks nothing reads exactly
        # like a gate that checked everything.
        echo "checking $(printf '%s\n' "$commits" | grep -c .) commit subject(s)"

        status=0
        for sha in $commits; do
          subject="$(git log -1 --format=%s "$sha")"
          # A space is legal inside the parentheses, because this repository
          # writes a multi-scope subject as `(nanopynix, ekn)`.
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
    };

    # The runner has to allow an unprivileged user namespace before any test
    # that unshares one can run, and the core pattern has to point somewhere a
    # later step can collect from.
    enable-sandbox-namespaces = mkStep {
      name = "ci-enable-sandbox-namespaces";
      runtimeInputs = with pkgs; [ util-linux ];
      text = ''
        sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
        sudo sysctl -w kernel.unprivileged_userns_clone=1
        sudo sysctl -w kernel.core_pattern=/tmp/core.%e.%p
        # Prove it took, here rather than in the first test that needs it.
        unshare --user --map-root-user --mount --pid --fork --mount-proc true
      '';
    };

    # The documentation build writes a store path, and `actions/upload-pages-
    # artifact` needs a writable directory.
    prepare-pages = mkStep {
      name = "ci-prepare-pages";
      runtimeInputs = with pkgs; [ coreutils ];
      text = ''
        mkdir -p public
        cp -r --no-preserve=mode,ownership result/. public/
      '';
    };

    # The version matrices of the scheduled workflow.
    #
    # **This embeds the answer rather than computing it at run time.** The
    # scheduled workflow runs `nix flake update` before it tests anything, so
    # the set of Nix versions is not knowable when the workflow is rendered: a
    # bumped nixpkgs can add or drop one. That used to mean five `nix eval`
    # calls in the step, each with a regular expression that repeated
    # `ci/variants.nix` in a second language. It does not have to: this script
    # is itself built from the updated flake, so `ciVersionMatrix` here is
    # already the updated answer and the step is one `echo` for each group.
    version-matrix = mkStep {
      name = "ci-version-matrix";
      text = ''
        : "''${GITHUB_OUTPUT:=/dev/stdout}"
        {
        ${lib.concatMapStringsSep "\n" (
          group: "echo ${lib.escapeShellArg "${group}_versions=${builtins.toJSON ciVersionMatrix.${group}}"}"
        ) (builtins.attrNames ciVersionMatrix)}
        } >> "$GITHUB_OUTPUT"
      '';
    };

    # **An abbreviated sha is the trap this catches.** `actions/checkout`
    # treats a `ref` that is not a full 40-character sha as a branch or a tag,
    # so `ref=59b837c26769` becomes `refs/heads/59b837c26769*`, which matches
    # nothing. git then retries for three minutes and fails with "exit code 1"
    # and no explanation. It cost a whole 30-job measurement once. Refuse it
    # here, and say why.
    check-dispatch-ref = mkStep {
      name = "ci-check-dispatch-ref";
      text = ''
        : "''${REF:?REF must name the branch, tag or full sha to check out}"
        if [[ "$REF" =~ ^[0-9a-f]{7,39}$ ]]; then
          echo "::error::ref '$REF' looks like an abbreviated commit sha. actions/checkout reads anything that is not a full 40-character sha as a branch or tag name, so this would fetch refs/heads/$REF and fail. Give a branch, a tag, or the full sha."
          exit 1
        fi
      '';
    };
  };
in
# One namespace, because a version name and a shared step name cannot collide:
# every version is `git` or `nix_<major>_<minor>` with an optional variant
# suffix, and every shared step is a hyphenated verb phrase.
versionSteps // sharedSteps
