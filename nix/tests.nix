{
  lib,
  writeShellApplication,
  python,
  nix,
  nixpkgs,
  gdb,
  pynix,
  tsanRuntime ? null,
}:
let
  pythonEnv = python.withPackages (
    _: pynix.dependencies ++ pynix.optional-dependencies.test ++ [ pynix ]
  );
in
writeShellApplication {
  name = "nanopynix-tests";
  runtimeInputs = [
    pythonEnv
    nix
    gdb
  ];
  text = ''
    cd ${lib.cleanSource ../.}
    export PYTHONNOUSERSITE=1
    export NIX_PATH=nixpkgs=${nixpkgs}
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
    export TSAN_OPTIONS="halt_on_error=1 history_size=7 second_deadlock_stack=1"
  ''
  + ''
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
      python -m pytest -p no:cacheprovider "$@" || status=$?
      if [ "$status" -gt 128 ]; then
        sig=$((status - 128))
        echo "python exited due to signal $sig; looking for a core dump matching $core_glob" >&2
        shopt -s nullglob
        # shellcheck disable=SC2206
        core_files=($core_glob)
        core_file=''${core_files[0]:-}
        if [ -n "$core_file" ]; then
          echo "=== gdb post-mortem backtrace from $core_file ===" >&2
          # No explicit executable argument -- gdb resolves the exact binary
          # from the core file's own recorded path. Passing python's PATH
          # symlink here instead silently broke symbol/shared-library
          # resolution for every frame in a real CI crash.
          gdb -q -batch -ex "thread apply all bt full" -ex quit -c "$core_file" >&2 || true
        else
          echo "no core file found matching $core_glob -- check kernel.core_pattern" >&2
        fi
      fi
      exit "$status"
    fi
    exec python -m pytest -p no:cacheprovider "$@"
  '';
  passthru = { inherit pythonEnv; };
}
