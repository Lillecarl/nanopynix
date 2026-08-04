# One sanitizer variant, applied to every C and C++ library that nanopynix's
# instrumented extension shares a process with.
#
# `name` picks the variant, and nothing else here differs between them: the
# reason each library needs its own override is the same whichever sanitizer
# is asking. See `sanitizers` in `default.nix` for the three call sites.
{
  lib,
  stdenv,
  # "thread", "undefined" or "address". Not a free-form flag string: the
  # runtime library, the shadow-memory rules and the set of libraries to
  # instrument all follow from which sanitizer this is.
  name,
}:
let
  isThread = name == "thread";
  isAddress = name == "address";
  isUndefined = name == "undefined";

  # ThreadSanitizer and AddressSanitizer both keep shadow memory, so the
  # runtime must be loaded before any other allocation happens in the process
  # (LD_PRELOAD). Otherwise the setup of that memory silently fails to
  # intercept everything. This bites us specifically because the instrumented
  # code is a native extension dlopen()'d into a plain, non-instrumented
  # CPython interpreter, not a from-scratch instrumented executable.
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
  # "version `GLIBC_ABI_DT_X86_64_PLT' not found". Measured, in the first ASAN
  # job, which never reported anything usable and was withdrawn.
  # `nanopynix/tests.nix` supplies its own git and its own coreutils, which
  # answers that for every variant here -- and the comment on `git` there
  # names this job as the reason it exists.
  runtime =
    if isThread then
      "${stdenv.cc.cc.lib}/lib/libtsan.so"
    else if isAddress then
      "${stdenv.cc.cc.lib}/lib/libasan.so"
    else
      null;

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

  # **The two boost defines tell ASAN that Nix switches stacks.** Nix dumps a
  # path on a coroutine, and ASAN knows nothing about the stack that coroutine
  # runs on. It then reads the bounds of the *thread* stack, subtracts, and
  # gets a negative size:
  #
  #   WARNING: ASan is ignoring requested __asan_handle_no_return:
  #     stack type: default top: 0x7ba812bd4f00; bottom 0x7fa828da3000;
  #     size: 0xfffffbffe9e31f00 (-4398417502464)
  #   False positive error reports may follow
  #
  # It then writes across that range, in `PlatformUnpoisonStacks`, and reports
  # its own write as a `stack-buffer-overflow` in `archive.cc`. Every frame of
  # the report belongs to `libasan.so`, and the address is a wild pointer. See
  # issue #60, which carries the measurement.
  #
  # ASAN supplies `__sanitizer_start_switch_fiber` and
  # `__sanitizer_finish_switch_fiber` for this, and boost calls both under
  # `BOOST_USE_ASAN`. **It calls them in one implementation only.**
  # `boost/context/fiber_ucontext.hpp` has 14 such sites and
  # `boost/context/fiber_fcontext.hpp` has none, and `boost/context/fiber.hpp`
  # picks `fcontext` unless `BOOST_USE_UCONTEXT` says otherwise. So both
  # defines are necessary, and neither is sufficient alone.
  #
  # The route is `src/libutil/serialise.cc` -> `boost/coroutine2/coroutine.hpp`
  # -> `pull_control_block_cc.ipp` -> `boost/context/fiber.hpp`. Two files of
  # Nix include boost this way, `serialise.cc` and `src/libexpr/eval-gc.cc`,
  # and both are meson components, so the flags below reach both. That matters:
  # the two implementations give `boost::context::fiber` a different layout, so
  # a build that used each in a different file would break the one-definition
  # rule. No header of Nix includes boost this way, so nanopynix-bindings needs
  # nothing.
  #
  # **The defines alone do not link, and `sanitizeBoost` below is the other
  # half.** `fiber_activation_record::current()` is `BOOST_CONTEXT_DECL`, so it
  # lives in the compiled library. nixpkgs builds boost.context with the
  # `fcontext` backend, and its `libboost_context.so` exports eight symbols:
  # five of `stack_traits`, and `jump_fcontext`, `make_fcontext` and
  # `ontop_fcontext`. None of `ucontext`. The first build with these defines
  # failed with ten undefined references to `current()`, and nothing else.
  #
  # `ucontext` costs a `sigprocmask` system call for each switch, which
  # `fcontext` does not make. Only this variant pays it.
  boostFiberFlags = [
    "-DBOOST_USE_UCONTEXT"
    "-DBOOST_USE_ASAN"
  ];

  sanitizerFlags = [
    "-fsanitize=${name}"
    "-fno-omit-frame-pointer"
    "-g"
  ]
  ++ lib.optional isUndefined "-fno-sanitize=vptr"
  # Last, and only for this variant. The order rule above is about a rebuild of
  # the whole instrumented closure, and an append that reaches one variant
  # leaves the string of the other two byte-identical.
  ++ lib.optionals isAddress boostFiberFlags;
  sanitizerFlagsStr = toString sanitizerFlags;

  # The value for meson's own `b_sanitize` option, or null to leave the option
  # alone. `mesonComponentOverrides` gives the reason this must exist beside
  # the compile flags above.
  #
  # The TSAN variant gets no value. `NIX_UBSAN_ENABLED` tests for `undefined`
  # alone, so a value of `thread` gives that build nothing.
  #
  # **The ASAN variant needs this one, and needs nothing else from it.**
  # libexpr reads `b_sanitize` to decide whether it may use the collector, and
  # it reads nothing else. The first ASAN variant of this file passed the flag
  # through `NIX_CFLAGS_COMPILE` alone, which meson never reads, so libexpr
  # built with ASAN *and* the collector -- and then reported a tag read of a
  # `Value` the collector had already freed. That report was not evidence of
  # anything. See `requiresNoGC` below.
  mesonSanitize = if isThread then null else name;

  # Whether sqlite and boehmgc get the flags too.
  #
  # **TSAN instruments them and the other two do not, and the asymmetry is the
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
  #
  # The ASAN variant says the same for sqlite, and does not reach boehmgc at
  # all: that build has no collector, so `sanitizeBoehmGC` below is never
  # called for it.
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
  suffix =
    if isThread then
      "tsan"
    else if isAddress then
      "asan"
    else
      "ubsan";

  # Environment for the *build* of nanopynix-bindings, beside the compile
  # flags. `nanopynix-bindings/package.nix` merges it into `env`.
  #
  # **ASAN needs `detect_leaks=0` here, and the `LD_PRELOAD` that `runtime`
  # above sets is the reason.** That preload is derivation-wide, because
  # stubgen and the import check both dlopen the extension into a fresh python
  # and a late dlopen cannot grow the TLS block. So it reaches `bash`, which
  # runs every phase, and LeakSanitizer runs at the exit of each of them.
  # Measured, in the first build of this variant: two reports, and every frame
  # of both was bash's own parser.
  #
  #   #1 0x5555555e30d0 in xmalloc  (.../bash-5.3p15/bin/bash+0x8f0d0)
  #   #2 0x55555558a5f6 in make_command
  #   #3 0x5555555868e3 in yyparse
  #   SUMMARY: AddressSanitizer: 2081 byte(s) leaked in 130 allocation(s)
  #
  # Only the leak checker goes. A use-after-free or a stack-use-after-return
  # in the import check still reports, and still fails the build, which is
  # what that phase is worth here. `nanopynix/tests.nix` sets its own, wider
  # `ASAN_OPTIONS` for the run of the suite.
  buildEnv = lib.optionalAttrs isAddress {
    ASAN_OPTIONS = "detect_leaks=0";
  };

  # Whether this variant forces `enableGC = false` on nix-expr.
  #
  # **Only ASAN.** `mesonFlags` below quotes the test that libexpr makes, and
  # `default.nix` asserts on this rather than letting the combination reach a
  # build: meson refuses an *enabled* `gc` feature with an error rather than
  # by disabling it, and that error arrives twenty minutes into a from-source
  # rebuild of the whole instrumented closure.
  #
  # **Nix 2.31 makes no such test, and this is where that matters.** Upstream
  # added `bdw_gc_required` after 2.31; that version's `src/libexpr/meson.build`
  # reads only `dependency('bdw-gc', required : get_option('gc'))`. So a 2.31
  # build of ASAN together with the collector *succeeds*, and produces exactly
  # the configuration whose report is not evidence. On 2.31 this attribute is
  # the only thing that stops it.
  requiresNoGC = isAddress;
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
      # **`address` here is legal only against a `-Dgc=disabled` libexpr.**
      # libexpr makes the test itself:
      #
      #   bdw_gc_required = get_option('gc').disable_if(
      #     'address' in get_option('b_sanitize'),
      #     error_message : 'Building with Boehm GC and ASAN is not supported')
      #
      # A conservative collector cannot see through the allocator of ASAN, so
      # the collector can free an object that is still live. `disable_if`
      # turns an *auto* `gc` feature off by itself, and fails an *enabled* one
      # with that message -- and nixpkgs writes `-Dgc=enabled`, never `auto`.
      # `requiresNoGC` above is how `default.nix` keeps the two in step.
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

  # Whether the nix components need a boost with the `ucontext` backend.
  # `boostFiberFlags` above holds the whole reason, and issue #60 holds the
  # measurement.
  needsUcontextBoost = isAddress;

  # boost is a plain buildInput of nix-util, nix-store and nix-expr, and all
  # three resolve it to `pkgs.boost`: this nixpkgs has no `boost` in the
  # `nixDependencies` scope, unlike `boehmgc` below.
  #
  # **All three take the same one, and that is not tidiness.** `BOOST_USE_ASAN`
  # adds three members to `fiber_activation_record`, and `BOOST_USE_UCONTEXT`
  # picks a different definition of it. A build where one library saw one
  # layout and another library saw the other breaks the one-definition rule,
  # and the two libraries share a process. `boostFiberFlags` above reaches
  # every meson component for the same reason.
  #
  # `define=BOOST_USE_ASAN` therefore goes to boost's own build as well. The
  # library compiles `fiber_activation_record_initializer`, which constructs
  # the struct, so the size has to agree with what the header gave nix.
  #
  # `linkflags=-fsanitize=address` links the ASAN runtime into
  # `libboost_context.so`, which the `__sanitizer_start_switch_fiber` and
  # `__sanitizer_finish_switch_fiber` calls of that header need. There is no
  # matching `cxxflags`, and the asymmetry is the point: boost gets the runtime
  # and not the instrumentation. `instrumentDependencies` above gives the
  # reason a report from third-party code is worth nothing here.
  sanitizeBoost =
    boost:
    boost.override {
      extraB2Args = [
        "context-impl=ucontext"
        "define=BOOST_USE_ASAN"
        "linkflags=-fsanitize=address"
      ];
    };

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
  # **The patch is not here any more, and it never belonged here.** It reached
  # the sanitized builds alone while it lived in this file, so the collector
  # that ships was the one collector with the abort still in it. It is in
  # `nix/boehmgc.nix` now, applied to every build with a collector, and that
  # file carries the reproduction from a plain dev shell. This function takes
  # an already patched collector and adds the instrumentation to it.
  sanitizeBoehmGC =
    boehmgc:
    boehmgc.overrideAttrs (
      old:
      dependencyEnv old
      // {
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
