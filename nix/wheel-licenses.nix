# The licence text of every library that rides in the wheel.
#
# A `manylinux` wheel carries 49 shared objects, and this project wrote five of
# them. Redistribution of the other 44 in binary form obliges this project to
# carry the licence and the copyright notice of each one. Almost every licence
# here says so directly: BSD, MIT, ISC, Apache-2.0, BSL-1.0 and the curl
# licence each require the notice to travel with a binary, and the two LGPL
# libraries require the text as well.
#
# **The list of libraries comes from the wheel, and the licence of each one
# comes from its own source.** Both halves matter, and both were wrong at
# first:
#
# - The build closure is not the wheel. `zlib`, `attr`, `lzo` and `libev` are
#   built by `nix/nix-closure.nix` and are *not* in the wheel: `zlib` is on the
#   `manylinux` whitelist, and the other three reach no shipped object. A
#   notice derived from the closure would claim a GPL library that is not
#   there. `sonames.tsv` below therefore maps a file name to a package, and
#   `nix/wheel.nix` reads it for the objects the wheel really holds.
# - `meta.license` in nixpkgs describes the *project*, and a wheel carries one
#   library out of it. Three entries disagree with the source, and `overrides`
#   below records each reading.
#
# ## Why the file list is written out and not found by a pattern
#
# A `find` for `COPYING*`, `LICEN[CS]E*` and `NOTICE*` was the first attempt.
# It is wrong in both directions, measured over these 30 sources:
#
# - It **misses**. `libxml2` keeps its licence in a file called `Copyright`,
#   and `boehmgc` has no licence file at all -- the text is a section of
#   `README.md`.
# - It **over-claims**, which is worse. `libgit2` vendors four dependencies
#   under `deps/`, so the pattern returns `deps/winhttp/COPYING.GPL` and
#   `deps/winhttp/COPYING.LGPL`. Neither builds here. A notice that names the
#   GPL for a library that does not carry it is a false statement about the
#   wheel. `boost` answers with 57 copies of one BSL text, `zstd` offers the
#   GPL-2 half of a dual licence, and `xz` offers three licences that cover
#   parts the wheel does not hold.
#
# So each entry names its files, and the build fails when one of them is not in
# the source. That turns an upstream rename into a build error rather than into
# a wheel with a licence file missing.
{
  lib,
  stdenvNoCC,
  runCommand,
  unzip,
}:

{
  # Every package that `nix/nix-closure.nix` rebuilt, plus the collector and the
  # Nix components. The soname map covers all of them, so a library that
  # becomes bundled later is named in the error rather than reported as
  # unknown.
  packages,
}:

let
  # Where the source disagrees with `meta.license` in nixpkgs. Each entry gives
  # the file that was read.
  overrides = {
    xz = {
      spdx = "0BSD";
      note = ''
        nixpkgs records `GPL-2.0-or-later, LGPL-2.1-or-later` for the xz
        project. The wheel carries `liblzma.so.5` alone, and `COPYING` says
        "liblzma is under the BSD Zero Clause License (0BSD)". `lzma.h` carries
        `SPDX-License-Identifier: 0BSD`. The GPL covers the xzgrep, xzdiff,
        xzless and xzmore scripts, and the LGPL covers a GNU getopt_long that
        this build does not compile. The wheel holds none of those.
      '';
    };
    acl = {
      spdx = "LGPL-2.1-or-later";
      note = ''
        nixpkgs records `GPL-2.0-or-later` for the acl project. The wheel
        carries `libacl.so.1` alone, and every source file of `libacl/` says
        "This library is free software; you can redistribute it and/or modify
        it under the terms of the GNU Lesser General Public License ... either
        version 2.1 of the License, or (at your option) any later version."
        The GPL covers the getfacl and setfacl tools.
      '';
    };
    nanopynix-cxx-runtime = {
      spdx = "GPL-3.0-or-later WITH GCC-exception-3.1";
      note = ''
        libstdc++ of gcc, which `nix/cxx-runtime.nix` links into one shared
        library with a private soname. nixpkgs records `GPL-3.0-or-later` for
        gcc, which is the compiler. `COPYING.RUNTIME` of the same source adds
        the GCC Runtime Library Exception to the runtime libraries, and that
        exception is what lets a product which links this library carry its own
        terms. nixpkgs has no attribute for the exception, so `meta.license`
        lists the two parts separately and would otherwise render as
        `GPL-3.0-or-later AND GCC-exception-3.1`, which says something else.
      '';
    };
    libgit2 = {
      spdx = "GPL-2.0-only WITH GCC-exception-2.0";
      note = ''
        nixpkgs records `GPL-2.0-only`, which is the licence without the
        exception that follows it. `COPYING` adds a LINKING EXCEPTION: "the
        authors give you unlimited permission to link the compiled version of
        this library into combinations with other programs, and to distribute
        those combinations without any restriction coming from the use of this
        file." So `libgit2.so.1` imposes no condition on the rest of the wheel.
        The GPL still governs modification of libgit2 itself.
      '';
    };
  };

  # The licence files of each source, by path from the root of the source.
  #
  # A package with no entry here is not in the wheel. `nix/wheel.nix` fails the
  # build when the wheel holds an object whose package has no entry, so this
  # list cannot fall behind the wheel silently.
  files = {
    # Nix itself, and the five libraries the extension links.
    nix-util = [ "COPYING" ];
    nix-store = [ "COPYING" ];
    nix-expr = [ "COPYING" ];
    nix-fetchers = [ "COPYING" ];
    nix-flake = [ "COPYING" ];

    # The collector. `README.md` whole, because the licence is the "Copyright &
    # Warranty" section of it and upstream ships no separate file.
    boehmgc = [ "README.md" ];

    # Compression and hashing.
    bzip2 = [ "LICENSE" ];
    brotli = [ "LICENSE" ];
    # `LICENSE` is the BSD-3 half. `COPYING` is the GPL-2 half of the dual
    # licence, and this wheel takes the BSD one.
    zstd = [ "LICENSE" ];
    # `COPYING` is the summary that says which licence covers which part, and
    # `COPYING.0BSD` is the one that covers liblzma. See `overrides.xz`.
    xz = [
      "COPYING"
      "COPYING.0BSD"
    ];
    # The C implementation offers three, and takes whichever the user prefers.
    libblake3 = [
      "LICENSE_A2"
      "LICENSE_A2LLVM"
      "LICENSE_CC0"
    ];

    # Storage and text.
    sqlite = [ "LICENSE.md" ];
    libxml2 = [ "Copyright" ];
    # sljit is the JIT backend, and it is compiled into `libpcre2-8.so`.
    pcre2 = [
      "LICENCE.md"
      "deps/sljit/LICENSE"
    ];
    # See `overrides.acl`. `doc/COPYING` is the GPL of the tools.
    acl = [ "doc/COPYING.LGPL" ];
    libarchive = [ "COPYING" ];
    libcpuid = [ "COPYING" ];
    libseccomp = [ "LICENSE" ];
    libsodium = [ "LICENSE" ];
    # The root file only. See the note above about `deps/`.
    libgit2 = [ "COPYING" ];
    boost = [ "LICENSE_1_0.txt" ];

    # Network.
    openssl = [ "LICENSE.txt" ];
    curl = [ "COPYING" ];
    libssh2 = [ "COPYING" ];
    nghttp2 = [ "COPYING" ];
    # sfparse is vendored and compiled in, and it carries its own copyright.
    nghttp3 = [
      "COPYING"
      "lib/sfparse/COPYING"
    ];
    ngtcp2 = [ "COPYING" ];
    # `LICENSE-MIT` covers the part derived from http_parser.
    llhttp = [
      "LICENSE"
      "LICENSE-MIT"
    ];

    # The AWS CRT, which gives `s3://` its authenticated requests. Apache-2.0
    # section 4(d) requires the NOTICE file to travel with a redistribution,
    # so every package that has one names it here. `aws-checksums` ships none.
    aws-c-common = awsLicense;
    aws-c-cal = awsLicense;
    aws-c-io = awsLicense;
    aws-c-compression = awsLicense;
    aws-c-http = awsLicense;
    aws-c-sdkutils = awsLicense;
    aws-c-auth = awsLicense;
    aws-c-s3 = awsLicense;
    aws-c-event-stream = awsLicense;
    aws-c-mqtt = awsLicense;
    aws-checksums = [ "LICENSE" ];
    s2n-tls = awsLicense;
    aws-crt-cpp = awsLicense;

    # The one C++ runtime of the closure. `nix/cxx-runtime.nix` says why the
    # wheel carries it. `COPYING3` is the licence and `COPYING.RUNTIME` is the
    # exception that makes a linked product distributable, so both travel.
    nanopynix-cxx-runtime = [
      "COPYING3"
      "COPYING.RUNTIME"
    ];
  };

  awsLicense = [
    "LICENSE"
    "NOTICE"
  ];

  spdxOf =
    package:
    lib.concatMapStringsSep " AND " (
      license: license.spdxId or license.fullName or license.shortName or "unknown"
    ) (lib.toList (package.meta.license or [ ]));

  # One derivation for each package, so a change to one licence entry rebuilds
  # that entry alone. Each unpacks the source that the wheel's library was
  # built from, which is the copy the notice has to describe.
  textOf =
    name: wanted:
    let
      package = packages.${name};
      # A package that carries its own licence text in its output, because it
      # has no source archive to unpack. No package needs it now: the C++
      # runtime is built from `libstdc++.a` and carries the source of gcc as
      # its own `src`, so the ordinary path below reads its two licence files
      # out of that tree.
      licenseRoot = package.licenseRoot or null;
    in
    if licenseRoot != null then
      runCommand "license-${name}" { } ''
        mkdir -p "$out"
        for wanted in ${lib.escapeShellArgs wanted}; do
          if [ ! -f "${package}/${licenseRoot}/$wanted" ]; then
            echo "wheel-licenses: ${name} installs no '$wanted' under ${licenseRoot}." >&2
            exit 1
          fi
          install -Dm444 "${package}/${licenseRoot}/$wanted" \
            "$out/$(echo "$wanted" | tr / _)"
        done
      ''
    else
      stdenvNoCC.mkDerivation {
        name = "license-${name}";
        inherit (package) src;
        nativeBuildInputs = [ unzip ];
        dontConfigure = true;
        dontBuild = true;
        dontFixup = true;
        # `sourceRoot` stays automatic. Some of these sources are a directory
        # from `fetchgit` and some are an archive, and the unpack phase of
        # stdenv handles both.
        installPhase = ''
          runHook preInstall
          mkdir -p "$out"
          for wanted in ${lib.escapeShellArgs wanted}; do
            if [ ! -f "$wanted" ]; then
              echo "wheel-licenses: the source of ${name} holds no '$wanted'." >&2
              echo "  Upstream renamed or moved it. Read the source, and correct the" >&2
              echo "  entry for ${name} in nix/wheel-licenses.nix. Do not delete the" >&2
              echo "  entry: the wheel ships this library, so the wheel needs the text." >&2
              exit 1
            fi
            # The path, and not the base name. `nghttp3` names two files
            # `COPYING`, and `install` would put the second one over the first.
            install -Dm444 "$wanted" "$out/$(echo "$wanted" | tr / _)"
          done
          runHook postInstall
        '';
      };

  texts = lib.mapAttrs textOf files;

  manifest = lib.mapAttrs (
    name: _:
    let
      package = packages.${name};
      override = overrides.${name} or { };
    in
    {
      version = package.version or "unknown";
      # The parentheses are load-bearing. `a.b or f x` applies the whole
      # `or` expression to `x`, so without them this reads
      # `(override.spdx or spdxOf) package` and calls a string.
      spdx = override.spdx or (spdxOf package);
      homepage = package.meta.homepage or null;
      note = override.note or null;
    }
  ) files;
in
runCommand "nanopynix-wheel-licenses"
  {
    passthru = { inherit manifest texts; };
    # **This describes the closure of a `manylinux` wheel, so it is Linux
    # only.** The soname loop below reads `lib/*.so*` of each rebuilt package,
    # and those packages include Linux-only libraries such as `acl`. Off Linux
    # the build stops on one of them, with a refusal that names that library
    # rather than this derivation.
    #
    # `flake.nix` filters `packages` by `lib.meta.availableOn`, so declaring
    # the platforms here is what keeps this out of the macOS package set.
    # Issue #148.
    meta.platforms = lib.platforms.linux;
  }
  ''
    mkdir -p "$out/texts"
    ${lib.concatStrings (
      lib.mapAttrsToList (name: text: ''
        cp -r ${text} "$out/texts/${name}"
      '') texts
    )}

    cp ${builtins.toFile "packages.json" (builtins.toJSON manifest)} "$out/packages.json"

    # Every soname of every rebuilt package, and not of the wheel. A library
    # that joins the wheel later resolves here, so `nix/wheel.nix` can name the
    # package that needs a licence entry instead of reporting an unknown file.
    : > "$out/sonames.tsv"
    ${lib.concatStrings (
      lib.mapAttrsToList (name: package: ''
        for object in ${lib.getLib package}/lib/*.so*; do
          [ -e "$object" ] || continue
          # A symbolic link is the development name of a library that the
          # wheel does not carry, and it resolves to a file already listed.
          [ -L "$object" ] && continue
          printf '%s\t%s\n' "$(basename "$object")" "${name}" >> "$out/sonames.tsv"
        done
      '') packages
    )}
  ''
