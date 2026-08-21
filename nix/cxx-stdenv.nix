# The stdenv that every library of the wheel is built with.
#
# It is the ordinary gcc stdenv of nixpkgs with two additions, and nothing else
# changes. That is the point of it: the zig stdenv it replaces needed a compiler
# wrapper, a boost toolset override, an include-order correction for libgit2, a
# `-march` translation for the AWS CRT and a hand-written `_Unwind_*` stub, and
# every one of those existed because zig is not the compiler that nixpkgs
# builds with. This file needs none of them.
#
# **1. Every C++ object links the private runtime instead of libstdc++.so.6.**
# `nix/cxx-runtime.nix` says why: `auditwheel` allows `libstdc++.so.6` and
# therefore caps the `GLIBCXX` version that the wheel may name at 3.4.29 for
# `manylinux_2_34`, which is gcc 11, and Nix 2.34 asks for C++23.
#
# The seam is `nix-support/libcxx-ldflags` of the cc-wrapper. `add-flags.sh`
# reads that file into `NIX_CXXSTDLIB_LINK`, and `bin/c++` appends that variable
# to `NIX_CFLAGS_LINK` for a C++ link and for no other. So a C compilation is
# untouched, which is correct: a C object never names libstdc++.
#
# `-L` reaches the ld-wrapper through the driver, and the ld-wrapper turns a
# store `-L` into an `RPATH` entry, so a consumer finds the runtime in the store
# as well as in the wheel.
#
# **`-lm` is part of the replacement, and leaving it out breaks the link.** The
# C++ spec of the gcc driver adds `-lstdc++ -lm` together, and `-nostdlib++`
# removes both. `libnanopynixcxx.so.1` names `libm.so.6` in `DT_NEEDED`, but a
# reference from the object being linked is not satisfied by a `DT_NEEDED` of
# another library: ld answers
# `undefined reference to symbol 'llround@@GLIBC_2.2.5'` and then
# `libm.so.6: error adding symbols: DSO missing from command line`.
#
# Measured: nghttp2 stopped exactly there, on `src/util.cc`, and every other
# C++ package of the closure that calls a libm function would have followed.
#
# **2. Every installed object has its glibc floor lowered.**
# `nix/lower-glibc.py` holds the measurement and the method. It runs here, at
# the fixup of each package, for two reasons that both make a later run
# impossible:
#
# - `auditwheel` reads each library through the RPATH of the extension, and
#   those paths are in the Nix store and read-only.
# - `auditwheel` refuses a tag it cannot support *before* it repairs, so a
#   rewrite after the repair is never reached.
#
# `postFixupHooks` and not `fixupOutputHooks`, because `stripDirs` and
# `patchELF` are themselves entries of the second one and both rewrite the file.
# The rewrite has to be last.
#
# **3. x86-64 uses the traditional TLS dialect, and not TLS descriptors.**
# GCC 15 changed the default on x86-64 to `-mtls-dialect=gnu2`. A TLS descriptor
# relocation makes the linker write a `GLIBC_ABI_GNU2_TLS` entry into
# `.gnu.version_r`, and glibc 2.41 is the first release that defines it.
#
# The entry carries no symbol, so the rewrite of `nix/lower-glibc.py` cannot
# reach it, and it must not: the relocations behind it are real. It is a request
# for a working implementation, and glibc corrected that implementation in 2.41.
#
# Measured on a probe with one `thread_local`:
#
#   <default>                 GLIBC_ABI_GNU2_TLS present
#   -mtls-dialect=gnu         absent
#   -mtls-dialect=gnu2        present
#
# Thirteen objects of the closure carried it, and `auditwheel` reported it
# beside `GLIBC_2.38` as a reason to refuse `manylinux_2_34`.
#
# **aarch64 is untouched, and that is measured and not assumed.** The same probe
# under aarch64 gcc 15 emits no marker at any dialect:
#
#   <default>                 absent
#   -mtls-dialect=trad        absent
#   -mtls-dialect=desc        absent
#
# The marker is a correction to the x86-64 implementation, and glibc defines the
# symbol there only. So aarch64 keeps TLS descriptors, which are faster, and the
# flag would buy nothing. `nix/lower-glibc.py` reports any `GLIBC_ABI_*` node it
# meets, so a later compiler that changes this fails the build that made it.
#
# `nix-support/cc-cflags` is the seam. `add-flags.sh` reads that file into
# `NIX_CFLAGS_COMPILE`, so a C compilation takes the flag as well as a C++ one.
# Both need it: TLS is not a C++ feature.
{
  lib,
  pkgs,
  stdenv ? pkgs.stdenv,
  cxxRuntime,
  nanopython,
  # The floor the wheel claims. `nix/wheel.nix` passes the same number to
  # `auditwheel`, and a mismatch fails that build rather than shipping a wheel
  # that does not load.
  glibcVersion ? "2.34",
}:

let
  # The script reads only the standard library, so it needs no environment of
  # its own. The shebang is written here rather than kept in the file, so that
  # `nix/lower-glibc.py` stays a module that the test suite can import.
  lowerGlibc =
    pkgs.runCommand "nanopynix-lower-glibc"
      {
        meta.mainProgram = "nanopynix-lower-glibc";
      }
      ''
        mkdir -p "$out/bin"
        {
          echo '#!${nanopython.interpreter}'
          cat ${./lower-glibc.py}
        } > "$out/bin/nanopynix-lower-glibc"
        chmod +x "$out/bin/nanopynix-lower-glibc"
      '';

  # The hook that each package of this stdenv sources. A file, and not a
  # heredoc inside `extraBuildCommands`: the indentation rules of a Nix
  # multi-line string and of a quoted heredoc disagree, and the shell then reads
  # a hook body that is indented into a syntax error.
  #
  # `find` groups its two names. Without the parentheses `-type f` binds to the
  # first `-name` alone, so every directory called `libfoo.so.1` would join the
  # list and the rewrite would fail on a directory.
  setupHook = pkgs.writeText "nanopynix-lower-glibc-hook.sh" ''
    _nanopynixLowerGlibcFloor() {
        local output prefix
        for output in $(getAllOutputNames); do
            prefix="''${!output}"
            [ -d "$prefix" ] || continue

            local objects=()
            mapfile -t objects < <(
                find "$prefix" -type f \( -name '*.so' -o -name '*.so.*' \)
            )
            [ "''${#objects[@]}" -eq 0 ] && continue

            ${lib.getExe lowerGlibc} --target ${glibcVersion} "''${objects[@]}"
        done
    }

    postFixupHooks+=(_nanopynixLowerGlibcFloor)
  '';

  # Empty on every other architecture, so nothing is written there.
  tlsFlags = lib.optionalString stdenv.hostPlatform.isx86_64 ''
    echo -mtls-dialect=gnu >> $out/nix-support/cc-cflags
  '';

  cc = stdenv.cc.override (old: {
    extraBuildCommands = (old.extraBuildCommands or "") + ''
      echo ${lib.escapeShellArg "-nostdlib++ -L${cxxRuntime}/lib -lnanopynixcxx -lm"} >> $out/nix-support/libcxx-ldflags

      ${tlsFlags}
      cat ${setupHook} >> $out/nix-support/setup-hook
    '';
  });
in
# An attrset, so a caller reaches the runtime and the floor by name. The zig
# stdenv that this replaces was consumed the same way.
pkgs.overrideCC stdenv cc
// {
  inherit cxxRuntime lowerGlibc glibcVersion;
}
