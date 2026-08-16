{
  lib,
  writeShellApplication,
  pythonSet,
  nix-cli,
  nixpkgs,
  coreutils,
  gdb,
  gitMinimal,
  tofuCoreSchemaTool,
  storeExecTools,
  version,
  sanitizer ? null,
  sanitizerRuntime ? null,
}:
let
  # The tree this runner `cd`s into. `nix/source.nix` says why it is filtered
  # and what a bare `../.` cost.
  source = import ../nix/source.nix { inherit lib; };

  # A venv over the whole repo, with every project's test extra enabled.
  #
  # This replaces a `python.withPackages` list that had to name each
  # project's `.dependencies` by hand, because `withPackages` drops
  # applications together with everything they propagate -- the bug that left
  # a native dependency out and a whole group of test modules uncollectable. A
  # venv has no such rule: `mkVirtualEnv` resolves the declared graph, so a
  # dependency cannot go missing without the resolution failing loudly.
  #
  # Built, not editable, unlike the dev shell. An editable install bakes an
  # absolute non-store path into the derivation, and this runner deliberately
  # `cd`s into a store copy of the tree below -- so an editable install here
  # would either point at a checkout that need not exist on the machine
  # running the tests, or (under flake evaluation, where the path *is* in the
  # store) be rejected by the renderer. Hermetic source and a live checkout
  # are alternatives; the runner is the hermetic one.
  #
  # `pytest-agent` is deliberately absent: it auto-activates on import, and
  # this runner is what CI executes.
  pythonEnv = pythonSet.mkVirtualEnv "nanopynix-test-env" {
    nanopynix = [ "test" ];
    nanopynix-helpers = [ ];
    pynix = [ "test" ];
  };

  # Interpolating this path literal copies it into the store and substitutes
  # the resulting store path below -- see nix/tsan-suppressions.txt for why
  # this specific, narrowly-scoped suppression is safe (a known-permanent
  # upstream Nix design choice, not a bug we're hiding).
  tsanSuppressions = ../nix/tsan-suppressions.txt;
in
(writeShellApplication {
  name = "nanopynix-tests";
  runtimeInputs = [
    pythonEnv
    nix-cli
    # mktemp/wc/head/rm, for bounding the post-mortem backtrace below.
    # writeShellApplication only *prepends* runtimeInputs to the ambient
    # PATH, so without this the crash path would silently depend on the host
    # having coreutils -- true on a GitHub runner, not something this runner
    # should rely on.
    coreutils
    gdb
    # Nix runs `git` off PATH to fetch a `git+file:` or a dirty `path:` flake
    # input, so `nanopynix/tests/bindings/test_flake.py` needs one. The host
    # git worked until the ASAN job, which sets LD_PRELOAD to the sanitizer
    # runtime. That runtime links against the glibc of this closure, and the
    # loader then gives the host git a mix of two glibcs:
    #
    #   git: /lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_ABI_DT_X86_64_PLT'
    #   not found (required by /nix/store/...-glibc-2.42-67/lib/libdl.so.2)
    #
    # so `test_eval_flake_writes_lock_file` failed. One git from this closure
    # answers that, and it removes a dependency on the host that the coreutils
    # entry above already rejects for the same reason.
    #
    # **Minimal, because the other half of git is perl.** `pkgs.git` carries a
    # 384.2 MiB closure over 87 store paths, and 40 of those paths are perl.
    # `pkgs.gitMinimal` carries 159.3 MiB over 34 paths, and none of them is
    # perl. What the minimal build drops is `git svn`, `git send-email`,
    # `git cvs*`, gitweb and the gui, and Nix calls none of them: the only
    # subcommand its fetcher names is `symbolic-ref`, and every core command
    # is present either way.
    gitMinimal
    # `pynix._lsp._tofu_core_schema` resolves `nanopynix-tofu-core-schema`
    # off PATH, so every core (non-provider) meta-argument hover/completion
    # needs it present. The dev shell and the released `pynix` app both
    # already supply it; this runner did not, which is why those scenarios
    # passed interactively and failed in CI -- `get_core_schema` catches the
    # OSError and returns None, so the LSP answered "no schema" rather than
    # erroring, and the tests read as a schema bug.
    tofuCoreSchemaTool
  ]
  # nanopynix.store_exec_prefix resolves this off PATH. Every Nix session in
  # this suite runs against a *relocated* store, so without it the terranix
  # LSP scenarios cannot exec `tofu` at all -- this is the runner's most
  # load-bearing case for it, not an edge case.
  #
  # Empty off Linux, where the tool does not exist. `default.nix` says why,
  # and the tests that need it carry a marker.
  ++ storeExecTools;
  text = ''
    cd ${source}
    export PYTHONNOUSERSITE=1
    export NIX_PATH=nixpkgs=${nixpkgs}
    # This runner is the gate, so it always takes the faithful path: every
    # pynix command opens its own worker, store and evaluator, exactly as the
    # real CLI does. The dev-shell default shares them across commands
    # (pynix/tests/_shared_sessions.py) -- ~25% faster on that suite, but by
    # construction blind to anything that needs a command to get a *fresh*
    # process, store handle or evaluator. That trade belongs in the local
    # edit-run loop, not in the run that decides whether a change is good.
    export NANOPYNIX_TEST_FAITHFUL_SESSIONS=1
  ''
  + lib.optionalString (sanitizerRuntime != null) ''
    # The sanitizer runtime must be loaded before any other allocation in the
    # process happens, or its shadow-memory interception silently misses
    # things -- this matters here because the instrumented code is a native
    # extension dlopen()'d into a plain, non-instrumented CPython interpreter,
    # not a from-scratch instrumented executable.
    export LD_PRELOAD="${sanitizerRuntime}''${LD_PRELOAD:+:$LD_PRELOAD}"
  ''
  + lib.optionalString (sanitizer != null) ''
    # A sanitizer reports on file descriptor 2, and the default `--capture=fd`
    # of pytest points that descriptor at a temporary file. `halt_on_error=1`
    # aborts the process before pytest prints the buffer, so the run says
    # nothing at all. `--capture=sys` keeps the descriptor. `tests/conftest.py`
    # refuses the `fd` method under a sanitizer, and gives the whole reason.
    #
    # PYTEST_ADDOPTS goes before the command line, so a job that passes
    # `--capture=no` still gets `no`.
    export PYTEST_ADDOPTS="--capture=sys''${PYTEST_ADDOPTS:+ $PYTEST_ADDOPTS}"
  ''
  + lib.optionalString (sanitizer != null && sanitizer.name == "thread") ''
    # halt_on_error=1: without it, a race hit deep in a loop (e.g. every
    # empty-attrset evaluation) gets re-reported on every single occurrence
    # instead of just the first, producing multi-million-line, unusable logs.
    # suppressions=...: silences the curlFileTransfer worker-thread "leak"
    # (nix/tsan-suppressions.txt) -- a permanent upstream design choice
    # (leaked-on-purpose singleton), not a bug, that otherwise shows up in
    # every TSAN run touching an HTTP fetch path.
    # die_after_fork=0: `nix daemon` forks a handler process per connection
    # and that child spawns its own worker threads -- exactly the pattern
    # TSAN's default die_after_fork=1 refuses ("starting new threads after
    # multi-threaded fork is not supported"). This is normal daemon behavior,
    # not a bug, so tell TSAN to tolerate it instead of aborting the child.
    export TSAN_OPTIONS="halt_on_error=1 history_size=7 second_deadlock_stack=1 die_after_fork=0 suppressions=${tsanSuppressions}"
  ''
  + lib.optionalString (sanitizer != null && sanitizer.name == "undefined") ''
    # halt_on_error: UBSan prints a violation and carries on by default, so
    # without this the job goes green with the report sitting in its log. It is
    # set here rather than compiled in with -fno-sanitize-recover because the
    # compile-time form also reaches sqlite's build-time code generator, where
    # upstream UB killed the build -- see nix/sanitizer.nix. This puts the
    # fatal boundary around the process under test, which is the thing being
    # gated.
    #
    # print_stacktrace: a UBSan report names one source line. The line that a
    # header gives is the line of the header, so the caller is the part that
    # says whose defect it is.
    export UBSAN_OPTIONS="print_stacktrace=1 halt_on_error=1"
  ''
  + lib.optionalString (sanitizer != null && sanitizer.name == "address") ''
    # detect_leaks=0: this build has no Boehm collector, because libexpr
    # refuses ASAN together with one. The evaluator therefore allocates and
    # never releases, on purpose, and the process is the unit of reclamation.
    # A leak checker here reports the design of the build rather than a
    # defect. This job is for a memory error.
    #
    # detect_stack_use_after_return=1 is not the default, and it is the exact
    # shape of the defect this job exists to catch: issue #34 was a
    # stack-local `nix::fetchers::Settings` whose address outlived its frame.
    #
    # halt_on_error=1: ASAN continues after a report by default, so without
    # this the job goes green with the finding sitting in its log. mkUbsanTestJob
    # in ci/workflows/lib.nix reads the log as well, for the case of a report
    # inside a forkserver worker that is reaped into a plain test failure.
    export ASAN_OPTIONS="detect_leaks=0 detect_stack_use_after_return=1 halt_on_error=1 print_stacktrace=1"
    # CPython allocates through its own arenas by default, and ASAN cannot see
    # inside one. A report then names the arena and not the caller.
    export PYTHONMALLOC=malloc
  ''
  + ''
    # Coverage is measured by `coverage run` and combined *after* pytest has
    # exited, rather than by pytest-cov. pytest-cov combines inside its
    # pytest_runtestloop hook -- while this suite still has live evaluator
    # threads and forkserver worker processes -- and that combine
    # intermittently died with `sqlite3.OperationalError: database is locked`
    # on the destination .coverage. It surfaced as an INTERNALERROR and exit
    # 3, which failed whole CI jobs *after every test had passed* and
    # swallowed the run's own failure summary on the way out. Proven
    # non-deterministic: two runs of the identical commit hit it in different
    # jobs, both times with a green test session behind it.
    #
    # Doing it as a separate step after the process exits removes the race by
    # construction: nothing else is alive to hold the database. Unset, this
    # is a plain pytest run, so the documented local `pytest --cov` workflow
    # is untouched.
    pytest_cmd=(python -m pytest -p no:cacheprovider)
    if [ -n "''${NANOPYNIX_COVERAGE:-}" ]; then
      pytest_cmd=(python -m coverage run -m pytest -p no:cacheprovider)
    fi

    # Reports the combine/report failure through the exit status rather than
    # swallowing it: a broken coverage pipeline should be visible, just not
    # by destroying an otherwise-complete test result.
    finish_coverage() {
      [ -n "''${NANOPYNIX_COVERAGE:-}" ] || return 0
      python -m coverage combine || return $?
      python -m coverage report --show-missing || return $?
      if [ -n "''${NANOPYNIX_COVERAGE_XML:-}" ]; then
        python -m coverage xml -o "$NANOPYNIX_COVERAGE_XML" || return $?
      fi
    }

    if [ -n "''${NANOPYNIX_CORE_DEBUG:-}" ]; then
      # Run at full speed (no ptrace attached -- a live gdb/strace attach
      # changes scheduling/timing enough to mask race-condition crashes).
      # Only on an actual crash do we reach for gdb, post-mortem, against
      # whatever core file the kernel produced. Relies on an absolute,
      # non-CWD-relative kernel.core_pattern (set by CI) so we don't need to
      # change directory -- that would break relative pytest path args.
      ulimit -c unlimited
      core_glob="''${NANOPYNIX_CORE_GLOB:-/tmp/core.*}"
      status=0
      "''${pytest_cmd[@]}" "$@" || status=$?
      if [ "$status" -gt 128 ]; then
        sig=$((status - 128))
        echo "python exited due to signal $sig; looking for a core dump matching $core_glob" >&2
        shopt -s nullglob
        # shellcheck disable=SC2206
        core_files=($core_glob)
        core_file=''${core_files[0]:-}
        if [ -n "$core_file" ]; then
          echo "=== gdb post-mortem backtrace from $core_file ===" >&2
          # Both limits exist because the crash this was built for is a stack
          # overflow: the faulting thread's stack is tens of thousands of
          # identical recursive frames, so an unbounded `thread apply all bt
          # full` produced millions of lines and swamped the GitHub Actions
          # log (and the uploaded artifact) with no added information.
          #
          # `bt $frames` rather than `bt`: the innermost frames identify a
          # runaway recursion just as well as all of them do, and the repeat
          # is the diagnosis. `bt` rather than `bt full`: this closure is
          # stripped, so `full` only adds a "No symbol table info available."
          # line per frame -- it multiplies the volume without adding detail.
          # Raise NANOPYNIX_CORE_BT_FRAMES/_LINES to widen either when a crash
          # genuinely needs more than the default.
          frames="''${NANOPYNIX_CORE_BT_FRAMES:-64}"
          max_lines="''${NANOPYNIX_CORE_BT_LINES:-2000}"
          gdb_log=$(mktemp)
          # No explicit executable argument -- gdb resolves the exact binary
          # from the core file's own recorded path. Passing python's PATH
          # symlink here instead silently broke symbol/shared-library
          # resolution for every frame in a real CI crash.
          #
          # The trailer below exists because `bt $frames` alone cannot tell a
          # stack overflow from an ordinary fault: a 2026-07-28 SIGSEGV in
          # nix::ExprVar::maybeThunk showed exactly `$frames` frames of varied
          # (non-repeating) evaluator frames, which is what both look like once
          # the cap truncates. `bt -3` names the outermost frames, and the
          # stack pointer against the crashing thread's own mapping is the
          # decisive test: an $sp within a page or so of the low end of its
          # region is an overflow, anywhere else is not.
          gdb -q -batch \
            -ex "set pagination off" \
            -ex "set print frame-arguments scalars" \
            -ex "thread apply all bt $frames" \
            -ex "echo \n=== crashing thread: outermost frames ===\n" \
            -ex "bt -3" \
            -ex "echo \n=== crashing thread: stack pointer vs mappings ===\n" \
            -ex "info registers rsp" \
            -ex "maintenance info sections" \
            -ex quit -c "$core_file" >"$gdb_log" 2>&1 || true
          total_lines=$(wc -l <"$gdb_log")
          head -n "$max_lines" "$gdb_log" >&2
          if [ "$total_lines" -gt "$max_lines" ]; then
            echo "=== $((total_lines - max_lines)) further backtrace lines suppressed (NANOPYNIX_CORE_BT_LINES=$max_lines) ===" >&2
          fi
          rm -f "$gdb_log"
        else
          echo "no core file found matching $core_glob -- check kernel.core_pattern" >&2
        fi
      fi
      # Only when pytest itself ran to completion: after a signal the data
      # files are truncated mid-write, and a combine failure there would
      # replace the crash's exit status with a far less informative one.
      if [ "$status" -le 128 ]; then
        finish_coverage || cov_status=$?
        [ "$status" -eq 0 ] && status="''${cov_status:-0}"
      fi
      exit "$status"
    fi
    status=0
    "''${pytest_cmd[@]}" "$@" || status=$?
    if [ "$status" -le 128 ]; then
      finish_coverage || cov_status=$?
      [ "$status" -eq 0 ] && status="''${cov_status:-0}"
    fi
    exit "$status"
  '';
  # There is no `addToMatrix` flag here any more. `ci/workflows/lib.nix` picked
  # the test runners out of the flat flake output set by that marker, because
  # a flake output set is flat and a name had to survive being flattened into
  # it. That file reads `ciVersionMatrix` from `default.nix` directly now, so
  # the marker had one producer and no consumer. `default.nix` carries the
  # reason a version must not be dropped from the matrix.
  passthru = {
    inherit pythonEnv;
  };
}).overrideAttrs
  {
    inherit version;
  }
