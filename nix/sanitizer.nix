# One sanitizer variant, applied to every C and C++ library that nanopynix's
# instrumented extension shares a process with.
#
# `name` picks the variant, and nothing else here differs between them: the
# reason each library needs its own override is the same whichever sanitizer
# is asking. See `sanitizers` in `default.nix` for the two call sites.
{
  lib,
  stdenv,
  # "thread" or "address". Not a free-form flag string: the runtime library
  # name and the shadow-memory rules follow from which sanitizer this is.
  name,
}:
let
  # The runtime must be loaded before any other allocation happens in the
  # process (LD_PRELOAD), or its shadow-memory setup silently fails to
  # intercept everything -- this bites us specifically because the
  # instrumented code is a native extension dlopen()'d into a plain,
  # non-instrumented CPython interpreter, not a from-scratch instrumented
  # executable.
  runtime = "${stdenv.cc.cc.lib}/lib/lib${if name == "address" then "asan" else "tsan"}.so";

  # UndefinedBehaviorSanitizer rides along with ASan and not with TSan,
  # deliberately. A UB finding is not thread-dependent, so running it under
  # both would report the same defect twice; and the TSan matrix skips 2.31
  # (see `default.nix`), which is the one version where the ownership rules
  # this is meant to catch differ. Attached here it runs everywhere.
  #
  # `vptr` is off because the check needs every class of a hierarchy
  # instrumented and CPython is not.
  #
  # **UBSan is deliberately not fatal at compile time.** `-fno-sanitize-recover`
  # was the first thing tried, and it broke the build rather than the tests:
  # these flags reach sqlite, whose build compiles a host code generator, and
  #
  #   tool/lemon.c:1795:3: runtime error: null pointer passed as argument 1,
  #   which is declared to never be null
  #
  # killed the sqlite derivation on every version. That is upstream UB in a
  # program that runs at build time and never at test time, so making it fatal
  # gates the wrong thing. The test runner sets `UBSAN_OPTIONS=halt_on_error=1`
  # instead, which puts the fatal boundary around the process under test and
  # leaves third-party build tooling alone.
  undefinedFlags = [
    "-fsanitize=undefined"
    "-fno-sanitize=vptr"
  ];

  sanitizerFlags = [
    "-fsanitize=${name}"
    "-fno-omit-frame-pointer"
    "-g"
  ]
  ++ lib.optionals (name == "address") undefinedFlags;
  sanitizerFlagsStr = toString sanitizerFlags;

  # The value for meson's own `b_sanitize` option. `mesonComponentOverrides`
  # gives the reason this must exist beside the compile flags above.
  mesonSanitize = if name == "address" then "address,undefined" else "thread";

in
{
  inherit name runtime;
  # The attribute-name suffix each variant gets in `nanopynixVersions`,
  # and therefore the job name in CI.
  suffix = if name == "address" then "asan" else "tsan";
  flags = sanitizerFlagsStr;
  # One token, no spaces. The compile flags go through
  # NIX_CFLAGS_COMPILE, which takes a string, but CMake's linker-flag
  # variables have to arrive as a single `-D...=` argument -- see
  # nanopynix-bindings/package.nix for what re-splits them otherwise.
  # Only the `-fsanitize=` list matters at link time; the rest is
  # instrumentation and debug info.
  linkFlag = "-fsanitize=${name}" + lib.optionalString (name == "address") ",undefined";

  # Applied via `nixComponents.overrideAllMesonComponents` so every nix-*
  # library (nix-util, nix-store, nix-expr, nix-fetchers, nix-cmd, ...) gets
  # consistent instrumentation -- a defect straddling two of these libraries
  # would be invisible if only one side were instrumented.
  mesonComponentOverrides = _finalAttrs: prevAttrs: {
    env = (prevAttrs.env or { }) // {
      NIX_CFLAGS_COMPILE = toString [
        (prevAttrs.env.NIX_CFLAGS_COMPILE or "")
        sanitizerFlagsStr
      ];
    };
    dontStrip = true;
    # "release"/"minsize" mesonBuildType auto-enables LTO (see nixpkgs'
    # nix modular packaging/components.nix), which both slows down TSAN's
    # instrumentation pass and makes reported stacks harder to read via
    # cross-TU inlining. "debugoptimized" keeps -O2 -g without LTO.
    mesonBuildType = "debugoptimized";

    # Nix decides the macro `NIX_UBSAN_ENABLED` from meson's own `b_sanitize`
    # option, and not from the compile flags above (`src/libutil/meson.build`).
    # That macro picks what `nixUnreachableWhenHardened` means, in
    # `src/libutil/include/nix/util/error.hh`. Three sites use the macro, and
    # all three are tag reads of a `Value` in `value.hh`.
    #
    # With the macro off, each site stays `std::unreachable()`, and UBSan
    # reports a trap against `<utility>:232` with no Nix source location and
    # no stack. With the macro on, each site becomes `nix::unreachable()`,
    # which prints the file and the line through `std::source_location`. This
    # is the reason upstream added the macro, and an instrumented build is the
    # case it exists for.
    #
    # The compile flags above stay. `b_sanitize` gives `-fsanitize=` only, and
    # the frame pointer, the debug info and the `vptr` exception come from
    # `NIX_CFLAGS_COMPILE`. A repeated `-fsanitize=` does no harm.
    mesonFlags = (prevAttrs.mesonFlags or [ ]) ++ [ (lib.mesonOption "b_sanitize" mesonSanitize) ];
  };

  # sqlite is a plain buildInput of nix-store (from outside the nix
  # component scope), not one of the meson components above, so it needs
  # its own override -- the crash we're chasing is inside sqlite3_step
  # itself, so if the actual race touches sqlite's own internal state
  # rather than just nix's wrapper code, an uninstrumented sqlite would
  # hide it from TSAN entirely.
  sanitizeSqlite =
    sqlite:
    sqlite.overrideAttrs (old: {
      env = (old.env or { }) // {
        NIX_CFLAGS_COMPILE = toString [
          (old.env.NIX_CFLAGS_COMPILE or "")
          sanitizerFlagsStr
        ];
      };
      dontStrip = true;
      # sqlite's own test suite fails under TSAN instrumentation (unrelated
      # to the race we're hunting in nix's usage of it) -- skip it rather
      # than debug sqlite's own tests here.
      doCheck = false;
      doInstallCheck = false;
    });

  # boehmgc is a plain buildInput of nix-expr (pkgs.boehmgc, resolved by
  # nixpkgs' own libexpr/package.nix callPackage, same as sqlite above), not
  # one of the meson components above. Instrumenting it alone (NIX_CFLAGS_
  # COMPILE below) did NOT fix or change the "pthread_kill failed at
  # suspend: errcode=22" abort observed under a broad multithreaded TSAN
  # pass -- confirmed by inspecting the actual TSAN nix-expr derivation's
  # propagated boehmgc buildInput (it did carry -fsanitize=thread) while
  # the crash stayed byte-identical. That's expected: this isn't a data
  # race in boehmgc's own compiled code that -fsanitize=thread could ever
  # catch -- it's boehm's own stop-the-world signaling a thread that has
  # already exited, a race undefined by POSIX (see
  # ./patches/boehmgc-tolerate-suspend-thread-exit-race.patch for the
  # actual fix). Keeping the instrumentation here anyway is still correct
  # in general: a genuine data race straddling boehmgc and nix-expr's own
  # code would otherwise be invisible to TSAN, same reasoning as sqlite.
  sanitizeBoehmGC =
    boehmgc:
    boehmgc.overrideAttrs (old: {
      env = (old.env or { }) // {
        NIX_CFLAGS_COMPILE = toString [
          (old.env.NIX_CFLAGS_COMPILE or "")
          sanitizerFlagsStr
        ];
      };
      patches = (old.patches or [ ]) ++ [ ./patches/boehmgc-tolerate-suspend-thread-exit-race.patch ];
      dontStrip = true;
      # gctest is boehmgc's own heavily multithreaded self-test -- exactly
      # the kind of workload that hits the stop-the-world suspend issue
      # we're chasing, so it can crash the *build* itself rather than
      # nanopynix's own test suite. Skip it here; nanopynix's own tests are
      # what we actually want to observe under TSAN.
      doCheck = false;
    });
}
