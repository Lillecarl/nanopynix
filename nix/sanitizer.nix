# One sanitizer variant, applied to every C and C++ library that nanopynix's
# instrumented extension shares a process with.
#
# `name` picks the variant, and nothing else here differs between them: the
# reason each library needs its own override is the same whichever sanitizer
# is asking. See `sanitizers` in `default.nix` for the two call sites.
{
  lib,
  stdenv,
  # "thread" or "undefined". Not a free-form flag string: the runtime library,
  # the shadow-memory rules and the set of libraries to instrument all follow
  # from which sanitizer this is.
  name,
}:
let
  isThread = name == "thread";

  # ThreadSanitizer keeps shadow memory, so its runtime must be loaded before
  # any other allocation happens in the process (LD_PRELOAD). Otherwise the
  # setup of that memory silently fails to intercept everything. This bites us
  # specifically because the instrumented code is a native extension
  # dlopen()'d into a plain, non-instrumented CPython interpreter, not a
  # from-scratch instrumented executable.
  #
  # UndefinedBehaviorSanitizer keeps no shadow memory. Its runtime supplies the
  # `__ubsan_handle_*` reporters that the instrumented code calls, and the link
  # of that code records the library as a dependency. The loader therefore
  # brings it in by itself, and `null` here says so.
  #
  # A preload of `libubsan.so` would also be a defect, and not only redundant.
  # LD_PRELOAD reaches every child process, and Nix runs `git` off PATH. A
  # preloaded library from this closure gives the host git the glibc of this
  # closure beside its own, and git then fails with
  # "version `GLIBC_ABI_DT_X86_64_PLT' not found". Measured, in the ASAN job
  # this variant replaces. `nanopynix/tests.nix` now supplies its own git,
  # which answers that for the TSAN variant too.
  runtime = if isThread then "${stdenv.cc.cc.lib}/lib/libtsan.so" else null;

  # The compile flags of this variant.
  #
  # `vptr` is off because the check needs every class of a hierarchy
  # instrumented, and CPython is not.
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
  #
  # **Keep this order.** The list becomes a string, the string goes into
  # `NIX_CFLAGS_COMPILE`, and that string is an input of every nix-* component.
  # A different order is the same compile line and a different derivation, so
  # it rebuilds the whole instrumented closure from source. That is 25 minutes
  # for the TSAN variant, and the cap of the TSAN job is 30, so a reorder alone
  # timed out two of the three TSAN jobs once. Measured, in run 30782379867.
  #
  # `-fno-sanitize=vptr` has to follow `-fsanitize=undefined`, so it goes last
  # rather than beside it.
  sanitizerFlags = [
    "-fsanitize=${name}"
    "-fno-omit-frame-pointer"
    "-g"
  ]
  ++ lib.optional (!isThread) "-fno-sanitize=vptr";
  sanitizerFlagsStr = toString sanitizerFlags;

  # The value for meson's own `b_sanitize` option, or null to leave the option
  # alone. `mesonComponentOverrides` gives the reason this must exist beside
  # the compile flags above.
  #
  # The TSAN variant gets no value. `NIX_UBSAN_ENABLED` tests for `undefined`
  # alone, so a value of `thread` gives that build nothing.
  mesonSanitize = if isThread then null else "undefined";

  # Whether sqlite and boehmgc get the flags too.
  #
  # **TSAN instruments them and UBSan does not, and the asymmetry is the
  # point.** A data race that straddles nix and one of these two libraries is
  # a defect of the way nanopynix uses the library, so it is ours to correct,
  # and an uninstrumented sqlite would hide it. Undefined behaviour inside
  # sqlite or inside boehmgc is upstream code that we cannot correct. sqlite
  # already proved this: its own build tool trips UBSan (see `sanitizerFlags`
  # above). boehmgc is a conservative collector, so it reads memory as
  # pointers by design, and that design is what UBSan asks about.
  #
  # `UBSAN_OPTIONS=halt_on_error=1` makes each report fatal, so an instrumented
  # third-party library turns this job red for a defect that nobody here can
  # correct. The gate would then say nothing. Instrument the code that we own,
  # and read a report from it as a defect of ours.
  instrumentDependencies = isThread;

  # Flags for a dependency that is not one of the meson components.
  dependencyEnv =
    old:
    lib.optionalAttrs instrumentDependencies {
      env = (old.env or { }) // {
        NIX_CFLAGS_COMPILE = toString [
          (old.env.NIX_CFLAGS_COMPILE or "")
          sanitizerFlagsStr
        ];
      };
    };

in
{
  inherit name runtime;
  # The attribute-name suffix each variant gets in `nanopynixVersions`,
  # and therefore the job name in CI.
  suffix = if isThread then "tsan" else "ubsan";
  flags = sanitizerFlagsStr;
  # One token, no spaces. The compile flags go through NIX_CFLAGS_COMPILE,
  # which takes a string, but CMake's linker-flag variables have to arrive as
  # a single `-D...=` argument -- see nanopynix-bindings/package.nix for what
  # re-splits them otherwise. Only the `-fsanitize=` list matters at link
  # time; the rest is instrumentation and debug info.
  linkFlag = "-fsanitize=${name}";

  # Applied via `nixComponents.overrideAllMesonComponents` so every nix-*
  # library (nix-util, nix-store, nix-expr, nix-fetchers, nix-cmd, ...) gets
  # consistent instrumentation -- a defect straddling two of these libraries
  # would be invisible if only one side were instrumented.
  mesonComponentOverrides =
    _finalAttrs: prevAttrs:
    {
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
    }
    # **The attribute is absent, and not empty, when there is nothing to add.**
    # `mesonFlags = prev ++ [ ]` writes the attribute even when the list does
    # not change, and a component that declared none then gains an empty one.
    # That is a different derivation, and a rebuild of the whole instrumented
    # closure. See `sanitizerFlags` above for what such a rebuild cost once.
    // lib.optionalAttrs (mesonSanitize != null) {
      # Nix decides the macro `NIX_UBSAN_ENABLED` from meson's own
      # `b_sanitize` option, and not from the compile flags above
      # (`src/libutil/meson.build`). That macro picks what
      # `nixUnreachableWhenHardened` means, in
      # `src/libutil/include/nix/util/error.hh`. Three sites use the macro,
      # and all three read the type tag of a `Value` in `value.hh`.
      #
      # With the macro off, each site stays `std::unreachable()`, and UBSan
      # reports a trap against `<utility>:232` with no Nix source location
      # and no stack. With the macro on, each site becomes
      # `nix::unreachable()`, which prints the file and the line through
      # `std::source_location`. This is the reason upstream added the macro,
      # and an instrumented build is the case it exists for.
      #
      # The compile flags above stay. `b_sanitize` gives `-fsanitize=` only,
      # and the frame pointer, the debug info and the `vptr` exception come
      # from `NIX_CFLAGS_COMPILE`. A repeated `-fsanitize=` does no harm.
      #
      # **This option must never name `address` here.** libexpr refuses that
      # combination:
      #
      #   bdw_gc_required = get_option('gc').disable_if(
      #     'address' in get_option('b_sanitize'),
      #     error_message : 'Building with Boehm GC and ASAN is not supported')
      #
      # A conservative collector cannot see through the allocator of ASAN, so
      # the collector can free an object that is still live. An ASAN variant
      # of this file therefore reported a tag read of a freed `Value` and
      # called it a finding. Issue #47 gives the supported route to ASAN,
      # which is a worker process that runs without the collector.
      mesonFlags = (prevAttrs.mesonFlags or [ ]) ++ [ (lib.mesonOption "b_sanitize" mesonSanitize) ];
    };

  # sqlite is a plain buildInput of nix-store (from outside the nix
  # component scope), not one of the meson components above, so it needs
  # its own override -- the crash we're chasing is inside sqlite3_step
  # itself, so if the actual race touches sqlite's own internal state
  # rather than just nix's wrapper code, an uninstrumented sqlite would
  # hide it from TSAN entirely. `instrumentDependencies` above says why the
  # UBSan variant takes the flags back off.
  sanitizeSqlite =
    sqlite:
    sqlite.overrideAttrs (
      old:
      dependencyEnv old
      // {
        dontStrip = true;
        # sqlite's own test suite fails under TSAN instrumentation (unrelated
        # to the race we're hunting in nix's usage of it) -- skip it rather
        # than debug sqlite's own tests here.
        doCheck = false;
        doInstallCheck = false;
      }
    );

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
  #
  # **The patch applies to both variants, and the flags do not.** The patch is
  # the correction to a real abort, so every sanitized build needs it. The
  # flags are the part that `instrumentDependencies` above takes back off.
  sanitizeBoehmGC =
    boehmgc:
    boehmgc.overrideAttrs (
      old:
      dependencyEnv old
      // {
        patches = (old.patches or [ ]) ++ [ ./patches/boehmgc-tolerate-suspend-thread-exit-race.patch ];
        dontStrip = true;
        # gctest is boehmgc's own heavily multithreaded self-test -- exactly
        # the kind of workload that hits the stop-the-world suspend issue
        # we're chasing, so it can crash the *build* itself rather than
        # nanopynix's own test suite. Skip it here; nanopynix's own tests are
        # what we actually want to observe under TSAN.
        doCheck = false;
      }
    );
}
