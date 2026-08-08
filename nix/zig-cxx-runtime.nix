# One C++ runtime for the whole zig closure.
#
# zig links libc++, libc++abi and libunwind **statically into every shared
# object**, and it compiles them with hidden visibility. So a process that
# loads the wheel holds one C++ runtime for each library: six of them, for the
# five Nix libraries and the extension. Each one has its own `std::locale`
# statics, its own `__cxa_eh_globals`, its own `operator new`, and its own
# unwinder.
#
# That is not a size problem. It is a correctness problem, because a C++ object
# that one library builds is destroyed by another. Issue #112 holds the
# measurement. The short form:
#
#   ~basic_format()   libnixexpr.so   calls operator delete on
#   std::__1::locale::__imp::classic_locale_imp_ in .bss of libnixutil.so
#
# libc++ refcounts the classic locale so that the count never reaches zero.
# That rule holds inside one libc++. It does not hold across two. `libnixexpr`
# released a static object of `libnixutil`, and glibc stopped the process with
# `double free or corruption`.
#
# **This derivation makes one shared runtime out of the archives that zig
# builds.** It does not compile libc++ again. The object code is the same code
# that zig would have linked into each library, so no configuration macro can
# disagree: `nix/zig-stdenv.nix` keeps every `-D_LIBCPP_*` that zig passes, and
# only the link changes.
#
# Three steps, and the middle one is the whole trick:
#
#   1. Make zig materialise `libc++.a` and `libc++abi.a`, by compiling one
#      trivial C++ file into its cache.
#   2. Give every hidden symbol default visibility. A hidden symbol cannot
#      leave the object that holds it, which is why each library ends up with
#      its own copy.
#   3. Link the two archives whole into one shared library.
#
# **The unwinder is not part of this, and it must not be.** zig also builds
# `libunwind.a`, and putting it here breaks every `throw`:
#
#   #4 _Unwind_RaiseException_Phase2         libgcc_s.so.1
#   #5 _Unwind_Resume                        libgcc_s.so.1
#   #6 nix::fromBoostUrlView                 libnixutil.so
#   #3 __gxx_personality_v0                  libnanopynixcxx.so.1
#   #1 unw_get_proc_info                     libnanopynixcxx.so.1  <- SIGSEGV
#
# `libpython` links `libgcc_s.so.1`, so that library is in the global scope
# before this one and it wins every lookup of `_Unwind_*`. A consumer then
# raises through the unwinder of gcc, which calls the personality routine that
# the frame names -- the one in here -- and that routine read the context of
# gcc with the reader of LLVM. **An unwinder and a personality routine are one
# implementation, and a process takes one of them.**
#
# So the unwinder is `libgcc_s.so.1` for everything. That is the ordinary way
# to build libc++abi on Linux, and it is sound here for a measured reason: the
# `libc++abi.a` of zig names eight `_Unwind_*` functions and no LLVM-internal
# `__unw_*`, and `libgcc_s.so.1` exports all eight. `manylinux_2_34` whitelists
# that library, so the wheel does not carry it and the host supplies it.
#
# **The link therefore takes a stub, and this file builds it.** A link needs
# eight names, one soname and one symbol version, and nothing else of the
# library. Two candidates that a reader would try first both fail:
#
#   -lgcc_s                zig reads `gcc_s` as a library that it supplies, and
#                          it answers the flag with its own static libunwind.
#                          Measured: no `_Unwind_Resume` in the result at all,
#                          and no `libgcc_s.so.1` in `DT_NEEDED`. It looks like
#                          a correct link, and it is the defect again.
#   the libgcc_s of gcc 15 it names `_dl_find_object@GLIBC_2.35`, which is one
#                          glibc release above the floor of this closure, so
#                          every consumer stops with an undefined reference.
#
# The stub carries the eight names at `GCC_3.0`, which is the version that
# `libgcc_s.so.1` has given them since 2001. It is a link input alone. It
# installs outside `$out/lib`, because `nix/zig-stdenv.nix` puts `$out/lib` on
# the `-L` line and the wrapper turns each `-L` into an `RPATH` entry. A
# `libgcc_s.so.1` on the `RPATH` of a consumer would load instead of the real
# one, and every `throw` would then return from an empty function.
#
# `-Bsymbolic` is necessary, and not a preference. Four lookup tables of
# `std::__1::__itoa` are read with a `R_X86_64_PC32` relocation. That
# relocation is legal against a hidden symbol and illegal against a preemptible
# one, so step 2 alone stops the link:
#
#   ld.lld: error: relocation R_X86_64_PC32 cannot be used against symbol
#   'std::__1::__itoa::__digits_base_10'; recompile with -fPIC
#
# `-Bsymbolic` makes every definition non-preemptible **inside this library**,
# which restores that relocation and still exports the symbol to everything
# else. The runtime keeps using its own statics, which is the point.
#
# `-Bsymbolic` has one consequence to know: a program that replaces the global
# `operator new` or `operator delete` replaces it for its own code and not for
# the code in here. Nothing in this closure replaces either one, and the
# collector does not: Nix reaches Boehm through the `gc_allocator` template,
# which becomes a call to `GC_malloc` at the point of use. Read
# `nix/zig-nix.nix` for that measurement.
#
# **The soname is this project's own, and not `libc++.so.1`.** The library
# rides in the wheel, and a wheel shares a process with other wheels. A second
# wheel that bundles its own libc++ under the ordinary name would collide with
# this one, and the collision would look exactly like the defect above.
{
  lib,
  stdenvNoCC,
  zig,
  llvmPackages,
  # The eight `_Unwind_*` functions that `libc++abi.a` names, and the symbol
  # version that `libgcc_s.so.1` gives each one. The header of this file says
  # why the link takes a stub and not the library.
  unwinderSymbols ? [
    "_Unwind_DeleteException"
    "_Unwind_GetIP"
    "_Unwind_GetLanguageSpecificData"
    "_Unwind_GetRegionStart"
    "_Unwind_RaiseException"
    "_Unwind_Resume"
    "_Unwind_SetGR"
    "_Unwind_SetIP"
  ],
  unwinderVersion ? "GCC_3.0",
  # The `<arch>-linux-gnu.<version>` triple of `nix/zig-stdenv.nix`. Both must
  # name one target, or the runtime and its consumers take different glibc
  # floors.
  target,
  soname ? "libnanopynixcxx.so.1",
}:

stdenvNoCC.mkDerivation {
  pname = "nanopynix-zig-cxx-runtime";
  inherit (zig) version;

  dontUnpack = true;
  strictDeps = true;

  nativeBuildInputs = [
    zig
    llvmPackages.bintools-unwrapped
  ];

  buildPhase = ''
        runHook preBuild

        # zig writes its archives under the cache, and it reads `HOME` when the
        # cache variables are absent. The sandbox has no writable `HOME`.
        export ZIG_GLOBAL_CACHE_DIR="$NIX_BUILD_TOP/zig-cache"
        export ZIG_LOCAL_CACHE_DIR="$NIX_BUILD_TOP/zig-local-cache"
        export HOME="$NIX_BUILD_TOP"

        # One translation unit that makes zig build the archives. zig builds
        # each one whole, so the contents of this file decide nothing beyond
        # "C++ was compiled".
        cat > probe.cc <<'PROBE'
    #include <exception>
    #include <locale>
    #include <sstream>
    int main() {
        std::ostringstream out;
        out.imbue(std::locale::classic());
        try {
            throw std::exception();
        } catch (const std::exception &) {
        }
        return 0;
    }
    PROBE
        zig c++ -target ${target} -o probe probe.cc

        # `libunwind` is deliberately absent. The header of this file gives the
        # backtrace that putting it here produces.
        for archive in libc++ libc++abi; do
          found=$(find "$ZIG_GLOBAL_CACHE_DIR" -name "$archive.a" -print -quit)
          if [ -z "$found" ]; then
            echo "zig-cxx-runtime: zig built no $archive.a" >&2
            exit 1
          fi
          cp "$found" "$archive.a"

          # Every `GLOBAL` or `WEAK` symbol that carries `HIDDEN`. A `LOCAL` one
          # names a file-scope helper, and it must stay local.
          llvm-readelf --symbols --wide "$archive.a" \
            | awk '($5 == "GLOBAL" || $5 == "WEAK") && $6 == "HIDDEN" { print $8 }' \
            | sed 's/@.*//' | sort -u > "$archive.syms"

          echo "zig-cxx-runtime: $archive.a, $(wc -l < "$archive.syms") symbols made visible"
          llvm-objcopy --set-symbols-visibility="$archive.syms"=default \
            "$archive.a" "visible-$archive.a"
        done

        # The stub that stands in for `libgcc_s.so.1` at link time. Each body is
        # empty, because a link reads the name and never the code. `-nostdlib`
        # is what keeps the stub free of an undefined symbol of its own, and
        # that is the whole point of building one.
        for symbol in ${lib.escapeShellArgs unwinderSymbols}; do
          echo "void $symbol(void) {}" >> unwind-stub.c
        done
        {
          echo "${unwinderVersion} {"
          echo "  global:"
          for symbol in ${lib.escapeShellArgs unwinderSymbols}; do
            echo "    $symbol;"
          done
          echo "  local: *;"
          echo "};"
        } > unwind-stub.map
        zig cc -target ${target} -shared -nostdlib \
          -Wl,--version-script=unwind-stub.map \
          -o libgcc_s.so.1 -Wl,-soname,libgcc_s.so.1 \
          unwind-stub.c

        # `-nostdlib++` keeps libc. This library calls `malloc`, `pthread_*` and
        # `__cxa_thread_atexit_impl`, so it names libc in `DT_NEEDED` like any
        # other shared library. `-nostdlib` would leave those undefined, and
        # they would resolve only because something else loaded libc first.
        zig c++ -target ${target} -shared -nostdlib++ \
          -Wl,-Bsymbolic \
          -o ${soname} -Wl,-soname,${soname} \
          -Wl,--whole-archive \
            visible-libc++.a visible-libc++abi.a \
          -Wl,--no-whole-archive \
          libgcc_s.so.1

        runHook postBuild
  '';

  # A gate, and not a report. Each symbol below is one failure of issue #112,
  # and a build that gets the visibility wrong links and then behaves exactly
  # like the defect it replaces.
  doCheck = true;
  checkPhase = ''
    runHook preCheck

    # The runtime owns these, and every consumer must reach the same one:
    # the locale static that the crash freed, the throw path, and the type
    # information class that the walk over base classes casts to.
    for symbol in \
      _ZNSt3__16locale5__imp19classic_locale_imp_E \
      __cxa_throw \
      _ZTVN10__cxxabiv120__si_class_type_infoE
    do
      if ! llvm-readelf --dyn-symbols --wide ${soname} \
           | awk -v s="$symbol" '$8 == s && $7 != "UND" { found = 1 } END { exit !found }'
      then
        echo "zig-cxx-runtime: ${soname} does not export $symbol" >&2
        exit 1
      fi
    done

    # **The unwinder is the opposite gate.** `libgcc_s.so.1` owns unwinding for
    # the process, so this library asks for it and must not answer for it. A
    # definition here would mean `libunwind.a` reached the link, and a `throw`
    # would then cross two unwinders and stop with SIGSEGV.
    # `sub` takes the version off the name. `libgcc_s.so.1` versions each of
    # these, so the reference reads `_Unwind_RaiseException@GCC_3.0` here and a
    # comparison against the bare name matches nothing.
    if ! llvm-readelf --dyn-symbols --wide ${soname} \
         | awk '{ sub(/@.*/, "", $8) }
                $8 == "_Unwind_RaiseException" && $7 == "UND" { found = 1 }
                END { exit !found }'
    then
      echo "zig-cxx-runtime: ${soname} defines _Unwind_RaiseException itself" >&2
      exit 1
    fi

    if ! llvm-readelf --dynamic --wide ${soname} | grep -q 'NEEDED.*libgcc_s\.so\.1'; then
      echo "zig-cxx-runtime: ${soname} does not name libgcc_s.so.1" >&2
      exit 1
    fi

    # The stub answers a link and nothing else, so it must need nothing itself.
    # A `DT_NEEDED` here would reach every consumer that links the stub.
    if llvm-readelf --dynamic --wide libgcc_s.so.1 | grep -q 'NEEDED'; then
      echo "zig-cxx-runtime: the libgcc_s.so.1 stub needs a library of its own" >&2
      llvm-readelf --dynamic --wide libgcc_s.so.1 >&2
      exit 1
    fi

    runHook postCheck
  '';

  installPhase = ''
    runHook preInstall

    # **Strip here, and do not leave it to `fixupPhase`.** `stdenvNoCC` carries
    # no binutils, so the automatic strip of stdenv does nothing and the
    # library installs at 9.4 MiB. It is 1.5 MiB stripped, and it rides in the
    # wheel. `.dynsym` is what every consumer reads, and a strip does not touch
    # it: only `.symtab` and the debug sections go.
    llvm-strip --strip-all ${soname}
    install -Dm555 -t "$out/lib" ${soname}
    # `-lnanopynixcxx` needs the unversioned name at link time.
    ln -s ${soname} "$out/lib/${lib.removeSuffix ".1" soname}"

    # **`link-stub`, and never `lib`.** `nix/zig-stdenv.nix` puts `$out/lib` on
    # the `-L` line, and the wrapper turns each `-L` into an `RPATH` entry. A
    # `libgcc_s.so.1` that a consumer can reach through its own `RPATH` would
    # load instead of the real one, and every `throw` would return from an
    # empty function.
    install -Dm444 -t "$out/link-stub" libgcc_s.so.1

    # The licence of each part, from the tree that the archives came from.
    # `nix/wheel-licenses.nix` reads these, because this library rides in the
    # wheel like every other one.
    for part in libcxx libcxxabi; do
      install -Dm444 "${zig}/lib/zig/$part/LICENSE.TXT" \
        "$out/share/licenses/$part/LICENSE.TXT"
    done

    runHook postInstall
  '';

  passthru = {
    inherit soname;
    # The path that a consumer names on its link line, so that the directory
    # above stays out of `nix/zig-stdenv.nix` and cannot be written as a `-L`.
    unwinderStub = "link-stub/libgcc_s.so.1";
    # This library rides in the wheel, so `nix/wheel-licenses.nix` has to carry
    # its text. That file reads the *source* of each package, and this one has
    # no source to unpack: it is built from the archives that zig materialises.
    # So it installs the licence file of each part itself, and names the
    # directory here. The two files differ, and both are necessary.
    licenseRoot = "share/licenses";
  };

  meta = {
    description = "libc++ and libc++abi of zig, as one shared library";
    # The runtimes of LLVM. Each `LICENSE.TXT` of the two says
    # "The LLVM Project is under the Apache License v2.0 with LLVM
    # Exceptions", so the expression is `Apache-2.0 WITH LLVM-exception`.
    # nixpkgs spells the exception as a separate entry.
    license = [
      lib.licenses.asl20
      lib.licenses.llvm-exception
    ];
    platforms = lib.platforms.unix;
  };
}
