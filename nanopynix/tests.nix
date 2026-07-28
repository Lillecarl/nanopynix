{
  lib,
  writeShellApplication,
  pythonSet,
  nix-cli,
  nixpkgs,
  coreutils,
  gdb,
  tofuCoreSchemaTool,
  storeExecTool,
  version,
  tsanRuntime ? null,
}:
let
  # A venv over the whole repo, with every project's test extra enabled.
  #
  # This replaces a `python.withPackages` list that had to name each
  # project's `.dependencies` by hand, because `withPackages` drops
  # applications together with everything they propagate -- the bug that left
  # `kr8s` out and four ekn test modules uncollectable. A venv has no such
  # rule: `mkVirtualEnv` resolves the declared graph, so a dependency cannot
  # go missing without the resolution failing loudly.
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
    ekn = [ ];
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
    # `pynix._lsp._tofu_core_schema` resolves `nanopynix-tofu-core-schema`
    # off PATH, so every core (non-provider) meta-argument hover/completion
    # needs it present. The dev shell and the released `pynix` app both
    # already supply it; this runner did not, which is why those scenarios
    # passed interactively and failed in CI -- `get_core_schema` catches the
    # OSError and returns None, so the LSP answered "no schema" rather than
    # erroring, and the tests read as a schema bug.
    tofuCoreSchemaTool
    # nanopynix.store_exec_prefix resolves this off PATH. Every Nix session in
    # this suite runs against a *relocated* store, so without it the terranix
    # LSP scenarios cannot exec `tofu` at all -- this is the runner's most
    # load-bearing case for it, not an edge case.
    storeExecTool
  ];
  text = ''
    cd ${../.}
    export PYTHONNOUSERSITE=1
    export NIX_PATH=nixpkgs=${nixpkgs}
    # This runner is the gate, so it always takes the faithful path: every
    # pynix command opens its own worker, store and evaluator, exactly as the
    # real CLI does. The dev-shell default shares them across commands
    # (tests/pynix/_shared_sessions.py) -- ~25% faster on tests/pynix, but by
    # construction blind to anything that needs a command to get a *fresh*
    # process, store handle or evaluator. That trade belongs in the local
    # edit-run loop, not in the run that decides whether a change is good.
    export NANOPYNIX_TEST_FAITHFUL_SESSIONS=1
  ''
  + lib.optionalString (tsanRuntime != null) ''
    # ThreadSanitizer's runtime must be loaded before any other allocation in
    # the process happens, or its shadow-memory interception silently misses
    # things -- this matters here because the instrumented code is a native
    # extension dlopen()'d into a plain, non-instrumented CPython interpreter,
    # not a from-scratch instrumented executable.
    export LD_PRELOAD="${tsanRuntime}''${LD_PRELOAD:+:$LD_PRELOAD}"
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
  passthru = {
    inherit pythonEnv;
    addToMatrix = true;
  };
}).overrideAttrs
  {
    inherit version;
  }
