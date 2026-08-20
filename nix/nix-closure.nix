# The Nix closure, rebuilt for a PyPI wheel.
#
# A wheel carries every library that the extension links, and a wheel takes the
# highest glibc floor of everything that it carries. The stdenv of nixpkgs puts
# that floor at `GLIBC_2.38`, and `nix/lower-glibc.py` says why the floor is
# gratuitous and how it comes off. Every library here moves onto
# `nix/cxx-stdenv.nix` together, which does two things to each one: it lowers
# the floor, and it makes each C++ object link the private runtime of
# `nix/cxx-runtime.nix` instead of `libstdc++.so.6`.
#
# Issue #111 holds the measurements.
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
  # `glibcVersion` is passed and not left to the default of that file. The
  # runtime lowers its own floor, so a floor that disagreed with the stdenv
  # would ship one library above the rest and `auditwheel` would refuse the tag.
  cxxRuntime = pkgs.callPackage ./cxx-runtime.nix { inherit glibcVersion; };

  cxxStdenv = import ./cxx-stdenv.nix {
    inherit (pkgs) lib python3;
    inherit pkgs cxxRuntime glibcVersion;
  };

  # The collector rides in the wheel like every other library, so it takes the
  # same floor. `patchBoehmGC` uses `overrideAttrs`, so `override` still reaches
  # the arguments of the package underneath.
  #
  # **`--enable-cplusplus` goes, and its two headers stay.** That flag builds
  # `libgccpp.so` and `libgctba.so`, and nothing links either one.
  #
  # Measured on the stock build of `libnixexpr.so.2.34.8`: it names `libgc.so.1`
  # and never `libgccpp`, and each of its 22 undefined collector symbols is a
  # plain C `GC_*`. Not one is a `_ZN2gc*`, which is what a class of `gc_cpp.h`
  # would give. So Nix uses the headers alone: `gc_allocator` is a template, and
  # it becomes a call to `GC_malloc` at the point of use.
  #
  # That measurement is also what makes `-Bsymbolic` safe in
  # `nix/cxx-runtime.nix`: nothing in this closure replaces the global
  # `operator new`, and the collector does not reach C++ allocation at all.
  #
  # But `src/libexpr/include/nix/expr/eval-gc.hh` does `#include` both headers,
  # and configure installs neither without the flag. So put the two headers
  # back, and leave the libraries out. Both say `#include "gc.h"` with quotes,
  # so the directory beside `gc.h` is the right one.
  wheelBoehmGC = (boehmgc.override { stdenv = cxxStdenv; }).overrideAttrs (old: {
    configureFlags = builtins.filter (flag: flag != "--enable-cplusplus") old.configureFlags;
    postInstall = (old.postInstall or "") + ''
      install -Dm444 -t "''${!outputDev}/include/gc" include/gc_cpp.h include/gc_allocator.h
    '';
  });

  # Every package whose shared object would ride in the wheel. It comes from the
  # 72 objects that the stock extension links, less what the trim below removes.
  #
  # **A C library is here for the floor, and a C++ library is here for the
  # runtime as well.** Neither reason lets a package stay behind: one gcc-built
  # library with an untouched floor holds the whole wheel at `GLIBC_2.38`.
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
    # either. Both are gated on the platform and not on an argument, so removing
    # them means an edit to the meson flags of Nix. Rebuilding them is smaller
    # than that, and it can still be revisited.
    "libcpuid"
    "libseccomp"
    # Only the examples of ngtcp2 link libev. It is small, so rebuilding it
    # costs less than removing the examples.
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
    # **Both, and `curlMinimal` is the one that carries the arguments.**
    # `pkgs.curl` is a wrapper whose one argument is `curlMinimal`, so an
    # `openssl` passed to it is dropped in silence and the wrapper keeps the
    # openssl of nixpkgs. `curl` is here because Nix links it, and
    # `curlMinimal` is here because it declares openssl, libssh2, nghttp2,
    # nghttp3, ngtcp2, zlib and zstd. `curl` declares `curlMinimal`, so the
    # fixpoint gives it the rebuilt one. Issue #220.
    "curlMinimal"
    "curl"
    "libarchive"
    "libgit2"
    "boost"

    # The AWS CRT, which gives `s3://` its authenticated requests. 13 libraries
    # and about 5 MiB.
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

  wheelLibs = import ./closure.nix {
    inherit lib pkgs;
    stdenv = cxxStdenv;
    inherit packages;

    extraArgs = {
      # **The trim of boost is gone, and it was gone before this comment.**
      # `pkgs.boost` and every `boostNNN` of this nixpkgs declare `callPackage`
      # and `fetchurl` alone: the version file calls `generic.nix` through
      # `callPackage`, and that call replaces the `.override` of the inner
      # package with its own. So `enableIcu = false` and `--without-stacktrace`
      # reached nothing, and the guard in `nix/closure.nix` is what said so.
      #
      # The measurement that the trim was worth: ICU was 39.1 MiB of the
      # payload, 46% of it, and it reaches the extension through `boost_regex`
      # and `boost_iostreams` only. Issue #220 holds the work to reach those
      # arguments again.

      curlMinimal = {
        # **This is what removes the last `strlcpy@GLIBC_2.38` of the closure.**
        # `nix why-depends` shows curl is the only path to krb5, and the krb5
        # libraries are the only ones left above `GLIBC_2.34` after the rewrite
        # of `nix/lower-glibc.py`. `strlcpy` and `strlcat` are real additions of
        # glibc 2.38, so a rename cannot reach them.
        #
        # It also drops keyutils, and about 3 MiB of payload.
        gssSupport = false;
        # `libidn2` has no `override` anywhere in nixpkgs, because nixpkgs
        # bootstraps `fetchurl` with it. So it cannot move onto this stdenv at
        # all, and its floor would hold the whole wheel. Dropping the feature
        # drops the library. IDN gives curl a host name that is not ASCII, and a
        # store URI does not use one.
        idnSupport = false;
        # The public suffix list decides which cookie a host may set. Nix keeps
        # no cookie, and this also drops libpsl, libxslt and libunistring.
        pslSupport = false;
      };

      # TBB gives blake3 a multithreaded hash of a large file, and it costs
      # onetbb and hwloc in the payload. A wheel that evaluates against a
      # `dummy://` store hashes no large file.
      libblake3 = {
        useTBB = false;
      };
    };

    extraAttrs = {
      # **The trim of ICU, reached through the attributes rather than the
      # arguments.** `.override` cannot carry `enableIcu` to boost in this
      # nixpkgs, and the comment above says why. The derivation still names
      # the library and the flag, so this removes both.
      #
      # ICU is 39.1 MiB of the payload, and it costs two more things that the
      # payload does not show. `libicuuc` is the one object of the closure that
      # demands `GLIBC_ABI_GNU2_TLS`, which `nix/lower-glibc.py` says a rewrite
      # cannot answer. The five ICU libraries are also the only objects that
      # link `libstdc++.so.6`, which `nix/cxx-runtime.nix` says the wheel does
      # not carry. Issue #220.
      boost = old: {
        buildInputs = builtins.filter (
          input: !(lib.hasInfix "icu4c" (input.name or ""))
        ) (old.buildInputs or [ ]);
        configureFlags = (
          builtins.filter (flag: !(lib.hasPrefix "--with-icu" flag)) (old.configureFlags or [ ])
        )
        ++ [ "--without-icu" ];
      };

      # **The second of the two symbols that a rename cannot reach.**
      # `arc4random_buf` arrived in glibc 2.36, and libarchive is the only
      # library of this closure that calls it. `nix/arc4random-compat.c` answers
      # the same name inside the C++ runtime, and that definition does not reach
      # a C library which resolves the symbol against glibc directly.
      #
      # So libarchive is built without it. Its configure script probes for the
      # function, and an `ac_cv_` cache variable answers the probe, so
      # libarchive falls back to the generator it uses on a host that has no
      # `arc4random_buf`.
      libarchive = old: {
        configureFlags = (old.configureFlags or [ ]) ++ [
          "ac_cv_func_arc4random_buf=no"
        ];
      };
    };
  };

  # The same shape as `applySanitizerOverrides` in `default.nix`, and for the
  # same reason: the fixpoint of the scope carries this to nanopynix-bindings,
  # so the extension links the libraries that this scope built.
  applyWheelOverrides =
    scope:
    scope.overrideScope (
      _final: prev:
      {
        stdenv = cxxStdenv;
        # `withAWS` keeps its default, which is on. The 13 libraries of the CRT
        # are in `packages` above, so they carry the same floor as the rest.
        #
        # This used to be `withAWS = false`, for about 5 MiB. That was the wrong
        # trade: an `s3://` binary cache is what some callers need the bindings
        # for, and a feature that a wheel cannot turn back on is a feature the
        # wheel does not have.
        #
        # The scope of a `nixComponents_X` holds no `boehmgc` attribute at all,
        # so naming it here would do nothing. libexpr takes it as an argument,
        # and that is where it has to go.
        nix-expr = prev.nix-expr.override { boehmgc = wheelBoehmGC; };

        # The extension itself. **`stdenv` above does not reach it.**
        #
        # `nanopynix-bindings/package.nix` takes `buildPythonPackage` and never
        # takes `stdenv`, and `buildPythonPackage` carries a stdenv of its own.
        # So the libraries moved and the extension did not: cmake reported the
        # ordinary compiler, the build finished, and the import then stopped
        # with an undefined `nix::Logger` symbol whose argument carried the
        # other spelling of `std::basic_string_view`. **A build that finishes is
        # not a build that works.**
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
          stdenv = cxxStdenv;
        };
      }
      // wheelLibs
      // {
        # **The wheel takes `curlMinimal`, and not the wrapper over it.**
        # `pkgs.curl` is `curlMinimal` with the features turned back on, so
        # `pslSupport = false` on `curlMinimal` reaches the wrapper as `true`
        # again and `libpsl` returns to the wheel. `libpsl` is then the last
        # object of the closure above `GLIBC_2.34`, and `libidn2` comes back
        # the same way. Nix fetches over HTTP and HTTPS and needs none of what
        # the wrapper adds. Issue #220.
        curl = wheelLibs.curlMinimal;
      }
    );
in
{
  inherit
    cxxRuntime
    cxxStdenv
    wheelLibs
    wheelBoehmGC
    packages
    applyWheelOverrides
    ;
}
