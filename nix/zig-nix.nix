# The Nix closure, rebuilt with the zig stdenv, for a PyPI wheel.
#
# A wheel carries every library that the extension links, and a wheel takes the
# highest glibc floor of everything that it carries. The stdenv of nixpkgs puts
# that floor at `GLIBC_2.38`, and `nix/zig-stdenv.nix` says why that number
# cannot be lowered with a compiler flag. So every library here moves onto the
# zig stdenv together.
#
# Issue #111 holds the measurements, and `nix/zig-stdenv.nix` and
# `nix/zig-cc-wrapper.sh` hold the corrections that the toolchain needs.
{
  lib,
  pkgs,
  # The collector that `default.nix` already patched. It must be that one, and
  # never `pkgs.boehmgc`: the `enableLargeConfig` and 1 MiB mark stack tuning
  # lives in the `nixDependencies` scope of nixpkgs, and the comment on
  # `patchedBoehmGC` in `default.nix` gives the reason a fresh one is wrong.
  boehmgc,
  # The interpreter that `callPythonPackage` in `default.nix` builds against.
  # It must be that one: `buildPythonPackage` below has to come from the same
  # interpreter set, or the extension builds for a different Python.
  python,
  glibcVersion ? "2.34",
}:
let
  zigStdenv = pkgs.callPackage ./zig-stdenv.nix { inherit glibcVersion; };

  # The collector rides in the wheel like every other library, so it takes the
  # same floor. `patchBoehmGC` uses `overrideAttrs`, so `override` still
  # reaches the arguments of the package underneath.
  #
  # **`--enable-cplusplus` has to go, and its two headers have to stay.**
  #
  # That flag builds `libgccpp.so` and `libgctba.so`. zig links libc++abi
  # statically into each shared library, with hidden visibility, so
  # `libgctba.so` cannot take `__cxa_throw` out of `libgccpp.so` and lld stops
  # with `non-exported symbol ... is referenced by DSO`. Dropping `-nostdlib`
  # does not answer this: the symbols stay hidden in both libraries either way.
  # It is what a static C++ runtime means, and not a fault of the link.
  #
  # Nothing links those two libraries. Measured on the stock build of
  # `libnixexpr.so.2.34.8`: it names `libgc.so.1` and never `libgccpp`, and
  # each of its 22 undefined collector symbols is a plain C `GC_*`. Not one is
  # a `_ZN2gc*`, which is what a class of `gc_cpp.h` would give. So Nix uses
  # the headers alone: `gc_allocator` is a template, and it becomes a call to
  # `GC_malloc` at the point of use.
  #
  # But `src/libexpr/include/nix/expr/eval-gc.hh` does `#include` both
  # headers, and configure installs neither without the flag. So put the two
  # headers back, and leave the libraries out. Both say `#include "gc.h"` with
  # quotes, so the directory beside `gc.h` is the right one.
  zigBoehmGC = (boehmgc.override { stdenv = zigStdenv; }).overrideAttrs (old: {
    configureFlags = builtins.filter (flag: flag != "--enable-cplusplus") old.configureFlags;
    postInstall = (old.postInstall or "") + ''
      install -Dm444 -t "''${!outputDev}/include/gc" include/gc_cpp.h include/gc_allocator.h
    '';
  });

  # Every package whose shared object would ride in the wheel. It comes from
  # the 72 objects that the stock extension links, less what the trim below
  # removes.
  packages = [
    # Leaves. Nothing here takes another package of this list.
    "zlib"
    "bzip2"
    "xz"
    "zstd"
    "brotli"
    "libsodium"
    "sqlite"
    "openssl"
    "attr"
    "acl"
    "pcre2"
    # Issue #111 lists these two under the trim, and the trim saves little on
    # either. Both are gated on the platform and not on an argument, so
    # removing them means an edit to the meson flags of Nix. Rebuilding them is
    # smaller than that, and it can still be revisited.
    "libcpuid"
    "libseccomp"
    # Only the examples of ngtcp2 link libev, and the gcc build of `libev.so`
    # needs `__isoc23_strtol@GLIBC_2.38`, which lld refuses. libev is small, so
    # rebuilding it costs less than removing the examples.
    "libev"

    # These take a leaf, or each other.
    "libssh2"
    "nghttp2"
    "nghttp3"
    "ngtcp2"
    "llhttp"
    "lzo"
    "libxml2"
    "libblake3"
    "curl"
    "libarchive"
    "libgit2"
    "boost"

    # The AWS CRT, which gives `s3://` its authenticated requests. 13 libraries
    # and about 5 MiB.
    #
    # **`aws-crt-cpp` is C++, so it cannot stay behind.** A gcc build of it
    # carries the libstdc++ ABI, and `libnixstore.so` is libc++ here, so the
    # two do not share a `std::string`. The C libraries below it move for the
    # ordinary reason: a gcc build holds the whole wheel at `GLIBC_2.38`.
    #
    # Nix 2.34 takes the CRT alone. `libstore/package.nix` reads
    # `versionAtLeast "2.33"` and takes `aws-sdk-cpp` only below that, so the
    # old and much larger SDK is not in this closure.
    "aws-c-common"
    "aws-c-cal"
    "aws-c-io"
    "aws-c-compression"
    "aws-c-http"
    "aws-c-sdkutils"
    "aws-c-auth"
    "aws-c-s3"
    "aws-c-event-stream"
    "aws-c-mqtt"
    "aws-checksums"
    "s2n-tls"
    "aws-crt-cpp"
  ];

  zigLibs = import ./zig-closure.nix {
    inherit lib pkgs zigStdenv packages;

    extraArgs = {
      boost = {
        # ICU is 39.1 MiB of the payload, 46% of it, and it reaches the
        # extension through boost_regex and boost_iostreams only.
        enableIcu = false;
        # boost reads `stdenv.cc.isClang` to pick its b2 toolset, and the cc of
        # zig answers `isZig` instead. boost then writes a `gcc` toolset into
        # `project-config.jam`, and b2 stops with `default command 'g++' not
        # found`. `zig cc` is clang, so name the toolset.
        toolset = "clang";
        extraB2Args = [
          # `boost_stacktrace_from_exception` interposes
          # `__cxa_allocate_exception`, and zig links libc++abi statically, so
          # lld answers `duplicate symbol`. nixpkgs already turns that library
          # off for libc++, and it reads `stdenv.cc.libcxx`, which is null here
          # because zig carries its own libc++ and not one of nixpkgs. Nix
          # links boost_context, boost_iostreams and boost_url, never
          # stacktrace.
          "--without-stacktrace"
          # b2 builds a precompiled header and then gives `pch.o` to the
          # linker, which answers `unknown file type`. Only `libs/math` asks
          # for one.
          "pch=off"
        ];
      };

      curl = {
        # krb5 and keyutils, about 3 MiB.
        gssSupport = false;
        # `libidn2` has no `override` anywhere in nixpkgs, because nixpkgs
        # bootstraps `fetchurl` with it. So it cannot move onto the zig stdenv
        # at all, and a gcc build of it would hold the whole wheel at
        # `GLIBC_2.38`. Dropping the feature drops the library. IDN gives curl
        # a host name that is not ASCII, and a store URI does not use one.
        idnSupport = false;
        # The public suffix list decides which cookie a host may set. Nix keeps
        # no cookie, and this also drops libpsl, libxslt and libunistring.
        pslSupport = false;
      };

      # TBB gives blake3 a multithreaded hash of a large file, and it costs
      # onetbb and hwloc in the payload. hwloc is also what proved that the
      # floor holds: its gcc build needs `__isoc23_strtoul@GLIBC_2.38`, and lld
      # refused the link. A wheel that evaluates against a `dummy://` store
      # hashes no large file.
      libblake3 = {
        useTBB = false;
      };
    };

    extraAttrs = {
      # libgit2 names its *own* header directory as a system one, with
      # `target_include_directories(xdiff SYSTEM PRIVATE ...)`. zig searches
      # its libc `-isystem` directories first, so the `regexp.h` of zig shadows
      # `src/util/regexp.h` and the build stops with `The GNU C Library no
      # longer implements <regexp.h>`. nixpkgs gives glibc to the compiler with
      # `-idirafter`, which is searched last, so the header of libgit2 wins
      # there.
      #
      # One `-I` puts the order back. Measured with zig 0.16: `-I` wins, and
      # both `-isystem` and `-idirafter` lose.
      libgit2 = old: {
        preConfigure = (old.preConfigure or "") + ''
          export NIX_CFLAGS_COMPILE="-I$PWD/src/util ''${NIX_CFLAGS_COMPILE:-}"
        '';
      };

      # zig 0.16 stops with `error: unsupported linker arg: --exclude-libs`.
      # Its driver keeps a list of the linker arguments that it forwards, and
      # this one is not on that list, although lld itself takes it.
      #
      # **The flag is a no-op here, so it goes rather than the wrapper.**
      # `AwsCFlags.cmake` says what it is for: it hides the symbols of
      # `libcrypto.a` so that a process holding both the static and the shared
      # libcrypto does not call one implementation for some functions and the
      # other for the rest. This closure links OpenSSL as a shared library, so
      # no `libcrypto.a` reaches the link and the flag hides nothing.
      #
      # One patch covers all 13 packages. `aws-c-common` installs this file to
      # `lib/cmake/aws-c-common/modules/`, and each of the other twelve does
      # `include(AwsCFlags)` against that installed copy. Putting the rule in
      # `nix/zig-cc-wrapper.sh` instead would change the hash of the compiler
      # and rebuild the whole closure on a shared machine.
      #
      # `AwsSIMD.cmake` is the second file, and it stops an aarch64 build.
      # It asks for the CRC and crypto instructions with
      # `-march=armv8-a+crc+crypto`, and zig reads `-march` on ARM as its own
      # `-mcpu`, takes `armv8` for a CPU name, and answers `unknown CPU:
      # 'armv8'` with a list of the names it knows.
      #
      # `-mcpu=generic+crc+crypto` is the same request in the spelling that zig
      # takes, so the hardware CRC stays. Measured with zig 0.16 on a file that
      # calls `__crc32cb` from `<arm_acle.h>`:
      #
      #   -march=armv8-a+crc+crypto    unknown CPU: 'armv8'
      #   -mcpu=generic+crc+crypto     compiles
      #
      # zig also refuses `-mtune=neoverse-v1`, so `check_c_compiler_flag` in
      # that file reports false and cmake takes the branch with no `-mtune`.
      # Both branches are replaced anyway.
      aws-c-common = old: {
        postPatch = (old.postPatch or "") + ''
          substituteInPlace cmake/AwsCFlags.cmake \
            --replace-fail ' -Wl,--exclude-libs,libcrypto.a' ""
          substituteInPlace cmake/AwsSIMD.cmake \
            --replace-fail '-march=armv8-a+crc+crypto' '-mcpu=generic+crc+crypto'
        '';
      };

      # `tests/fullbench.c` expands `__DATE__`. clang reports `-Wdate-time` for
      # that and gcc does not, so `-Werror` stops the build of the test alone.
      # The library compiles, and the test suite still runs.
      zstd = old: {
        env = (old.env or { }) // {
          NIX_CFLAGS_COMPILE = "${old.env.NIX_CFLAGS_COMPILE or ""} -Wno-error=date-time";
        };
      };
    };
  };

  # The same shape as `applySanitizerOverrides` in `default.nix`, and for the
  # same reason: the fixpoint of the scope carries this to nanopynix-bindings,
  # so the extension links the libraries that this scope built.
  applyZigOverrides =
    scope:
    scope.overrideScope (
      _final: prev:
      {
        stdenv = zigStdenv;
        # `withAWS` keeps its default, which is on. The 13 libraries of the CRT
        # are in `packages` above, so they carry the same floor as the rest.
        #
        # This used to be `withAWS = false`, for about 5 MiB. That was the
        # wrong trade: an `s3://` binary cache is what some callers need the
        # bindings for, and a feature that a wheel cannot turn back on is a
        # feature the wheel does not have.
        # The scope of a `nixComponents_X` holds no `boehmgc` attribute at all,
        # so naming it here would do nothing. libexpr takes it as an argument,
        # and that is where it has to go.
        nix-expr = prev.nix-expr.override { boehmgc = zigBoehmGC; };

        # The extension itself. **`stdenv` above does not reach it.**
        #
        # `nanopynix-bindings/package.nix` takes `buildPythonPackage` and never
        # takes `stdenv`, and `buildPythonPackage` carries a stdenv of its own.
        # So the libraries moved and the extension did not: cmake reported
        # `The CXX compiler identification is GNU 15.3.0`, the build finished,
        # and the import then stopped with
        # `undefined symbol: _ZN3nix6Logger13writeToStdoutESt17basic_string_view...`.
        # `St17basic_string_view` is the libstdc++ spelling of that argument,
        # and a libc++ `libnixutil.so` exports the `NSt3__1...` one. **A build
        # that finishes is not a build that works**, a second time.
        #
        # `callPythonPackage` in `default.nix` reads
        # `pkgs // python.pkgs // ... // final`, so an attribute of the scope is
        # the last word and shadows the one of the interpreter set.
        #
        # `.override` is the supported route. `mk-python-derivation.nix` says
        # `Allow passing in a custom stdenv to buildPython*.override` above the
        # argument, and `stdenv` inside the attribute set is deprecated and
        # warns.
        buildPythonPackage = python.pkgs.buildPythonPackage.override {
          stdenv = zigStdenv;
        };
      }
      // zigLibs
    );
in
{
  inherit
    zigStdenv
    zigLibs
    zigBoehmGC
    packages
    applyZigOverrides
    ;
}
