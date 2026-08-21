# One C++ runtime for the whole wheel closure, out of the `libstdc++.a` of gcc.
#
# **Why the wheel cannot use `libstdc++.so.6`.** `auditwheel` allows that
# library and does not bundle it, so the host supplies it and the policy caps
# the symbol versions that the wheel may name. Measured from the policy file of
# auditwheel 6.7.0:
#
#   manylinux_2_34   GLIBC <= 2.34   GLIBCXX <= 3.4.29   CXXABI <= 1.3.13
#   manylinux_2_35   GLIBC <= 2.35   GLIBCXX <= 3.4.30   CXXABI <= 1.3.13
#
# `GLIBCXX_3.4.29` is gcc 11, and `3.4.30` is gcc 12. Nix 2.34 asks for
# `cpp_std=c++23` in every `meson.build` of every component, so gcc 11 cannot
# build it. A shared libstdc++ therefore caps this wheel at `manylinux_2_35`,
# and only if gcc 12 compiles the C++23 of Nix.
#
# **This derivation removes the cap instead.** It links `libstdc++.a` whole into
# one shared library with a name of this project, and `nix/cxx-stdenv.nix` makes
# every C++ object of the closure link that library rather than
# `libstdc++.so.6`. A symbol that arrives this way carries no `GLIBCXX` version
# at all, so no policy caps it. Measured on a probe that throws, catches, uses
# `dynamic_cast`, a stream and `std::random_device`:
#
#   shared libstdc++   NEEDED libstdc++.so.6   GLIBCXX 3.4 .. 3.4.31, CXXABI 1.3, 1.3.9
#   this runtime       NEEDED libnanopynixcxx.so.1   GLIBCXX none, CXXABI none
#
# **One shared runtime, and not `-static-libstdc++` in each object.** Static
# linking in each object gives each one its own `std::locale` statics, its own
# `__cxa_eh_globals` and its own `operator new`. Issue #112 holds what that
# costs: a C++ object that one library builds is destroyed by another, and glibc
# stops the process with `double free or corruption`. The gates of
# `nix/wheel-gates.py` read the finished wheel for exactly that shape.
#
# **The unwinder is `libgcc_s.so.1`, and this library only asks for it.** A
# process takes one unwinder, because the unwinder and the personality routine
# are one implementation. `libpython` already links `libgcc_s.so.1`, so that
# library wins every lookup of `_Unwind_*` whatever this one does.
# `manylinux_2_34` allows it, so the wheel does not carry it and the host
# supplies it.
#
# That is the whole reason this file is shorter than the zig runtime it
# replaces. zig answered `-lgcc_s` with its own static libunwind, so the link
# needed a hand-written stub of eight `_Unwind_*` names at `GCC_3.0`. gcc needs
# none: `-lgcc_s` links the real library, and the references come out at the
# versions that library has given them since 2001. Measured on this runtime:
#
#   UND _Unwind_RaiseException@GCC_3.0
#   UND _Unwind_Resume@GCC_3.0
#   UND _Unwind_Resume_or_Rethrow@GCC_3.3
#
# **`-Bsymbolic` is necessary, and not a preference.** It makes every definition
# non-preemptible inside this library, so the runtime keeps using its own
# statics, and it still exports each symbol to every consumer.
#
# One consequence to know: a program that replaces the global `operator new` or
# `operator delete` replaces it for its own code and not for the code in here.
# Nothing in this closure replaces either one. Nix reaches the collector through
# the `gc_allocator` template, which becomes a call to `GC_malloc` at the point
# of use, and `nix/nix-closure.nix` holds that measurement.
#
# **The soname is this project's own, and not `libstdc++.so.6`.** The library
# rides in the wheel, and a wheel shares a process with other wheels. A second
# wheel that bundles a C++ runtime under the ordinary name would collide with
# this one, and the collision looks exactly like issue #112.
{
  lib,
  stdenv,
  nanopython,
  # **This derivation lowers its own floor, and nothing else does it.**
  #
  # `nix/cxx-stdenv.nix` installs the rewrite as a setup hook, and every package
  # of the closure gets it from there. This one cannot: that stdenv is built
  # *from* this derivation, so taking it here is a cycle. It therefore uses the
  # ordinary stdenv and calls the rewrite itself.
  #
  # Measured, and this is how the omission was found: the first wheel build
  # reached `auditwheel`, and every object of the closure was at or below
  # `GLIBC_2.34` except this one, which kept `__isoc23_strtoul@GLIBC_2.38`.
  glibcVersion ? "2.34",
  # `libstdc++.a` of this compiler holds libsupc++ as well, so the link takes
  # one archive. Naming `libsupc++.a` beside it gives `multiple definition` for
  # every type information class and every `__cxa_*` entry point.
  soname ? "libnanopynixcxx.so.1",
}:

stdenv.mkDerivation {
  pname = "nanopynix-cxx-runtime";
  inherit (stdenv.cc.cc) version;

  # The source of the compiler, and this derivation never unpacks it.
  # `nix/wheel-licenses.nix` reads `src` of every package that rides in the
  # wheel, and it takes `COPYING3` and `COPYING.RUNTIME` out of this tree. The
  # text has to come from the source that built the library, and the store
  # output of gcc carries no licence file at all.
  src = stdenv.cc.cc.src;
  dontUnpack = true;
  strictDeps = true;

  nativeBuildInputs = [ nanopython ];

  buildPhase = ''
    runHook preBuild

    archive=$($CXX -print-file-name=libstdc++.a)
    if [ ! -e "$archive" ]; then
      echo "cxx-runtime: the compiler supplies no libstdc++.a" >&2
      exit 1
    fi
    echo "cxx-runtime: linking $archive"

    # Before the archive, so that the linker binds the `arc4random` of
    # `std::random_device` to this definition and writes no `@GLIBC_2.36`
    # reference. The header of that file gives the measurement.
    $CC -fPIC -c ${./arc4random-compat.c} -o arc4random-compat.o

    # `-nostdlib++` keeps libc and drops the implicit `-lstdc++` of the driver.
    # Without it the driver adds the shared libstdc++ underneath the archive,
    # and the runtime then names the library it exists to replace.
    #
    # `-lm` is needed because `-nostdlib++` also drops it, and libstdc++ calls
    # `fegetround`. A link without it succeeds and the first `dlopen` reports
    # `undefined symbol: fegetround`.
    $CXX -shared -nostdlib++ \
      -o ${soname} -Wl,-soname,${soname} \
      -Wl,-Bsymbolic \
      arc4random-compat.o \
      -Wl,--whole-archive "$archive" -Wl,--no-whole-archive \
      -lgcc_s -lm

    # The rewrite that every other package of the closure gets from the setup
    # hook of `nix/cxx-stdenv.nix`. The head of this file says why it has to
    # happen here instead. `libstdc++.a` calls `strtoul` and `sscanf`, so the
    # link writes `__isoc23_*@GLIBC_2.38` without this line.
    ${nanopython.interpreter} ${./lower-glibc.py} --target ${glibcVersion} ${soname}

    runHook postBuild
  '';

  # A gate, and not a report. Each check below is one failure that produces a
  # library which links, installs, and then behaves like the defect it replaces.
  doCheck = true;
  checkPhase = ''
    runHook preCheck

    # The runtime owns these, and every consumer must reach the same one: the
    # throw path, the type information class that a walk over base classes casts
    # to, and the personality routine that each frame names.
    for symbol in __cxa_throw _ZTVN10__cxxabiv120__si_class_type_infoE __gxx_personality_v0; do
      if ! readelf --dyn-syms --wide ${soname} \
           | awk -v s="$symbol" '$8 == s && $7 != "UND" { found = 1 } END { exit !found }'
      then
        echo "cxx-runtime: ${soname} does not export $symbol" >&2
        exit 1
      fi
    done

    # **The unwinder is the opposite gate.** `libgcc_s.so.1` owns unwinding for
    # the process, so this library asks for it and must not answer for it. A
    # definition here would mean a second unwinder in the process, and a throw
    # would then cross both and stop with SIGSEGV.
    #
    # `sub` takes the version off the name, because the reference reads
    # `_Unwind_RaiseException@GCC_3.0` and a comparison against the bare name
    # matches nothing.
    if ! readelf --dyn-syms --wide ${soname} \
         | awk '{ sub(/@.*/, "", $8) }
                $8 == "_Unwind_RaiseException" && $7 == "UND" { found = 1 }
                END { exit !found }'
    then
      echo "cxx-runtime: ${soname} defines _Unwind_RaiseException itself" >&2
      exit 1
    fi

    if ! readelf --dynamic --wide ${soname} | grep -q 'NEEDED.*libgcc_s\.so\.1'; then
      echo "cxx-runtime: ${soname} does not name libgcc_s.so.1" >&2
      exit 1
    fi

    # The library this one replaces. Naming it would defeat the whole
    # derivation, and `-nostdlib++` is the one flag that keeps it out.
    if readelf --dynamic --wide ${soname} | grep -q 'NEEDED.*libstdc++\.so\.6'; then
      echo "cxx-runtime: ${soname} names libstdc++.so.6, so -nostdlib++ was lost" >&2
      exit 1
    fi

    # The floor of this library, checked in the build that makes it. The script
    # above already fails on a symbol it cannot rename, and this reads the
    # finished file instead of trusting that. `libnanopynixcxx.so.1` was the one
    # object of the whole closure that reached `auditwheel` unlowered, and the
    # error named the wheel rather than this derivation.
    #
    # `--version-info` reads `.gnu.version_r` as well as the symbol table, so a
    # node that keeps the floor high with no symbol behind it is also caught.
    # `sort -V` over the two names gives the comparison: the target sorts last
    # when the object is at or below it.
    highest=$(
      readelf --version-info --dyn-syms --wide ${soname} \
        | grep -oE 'GLIBC_[0-9][0-9.]*' | sort -u -V | tail -n 1
    )
    if [ "$(printf '%s\nGLIBC_${glibcVersion}\n' "$highest" | sort -V | tail -n 1)" \
         != "GLIBC_${glibcVersion}" ]; then
      echo "cxx-runtime: ${soname} needs $highest, above GLIBC_${glibcVersion}" >&2
      readelf --version-info --dyn-syms --wide ${soname} | grep -E "$highest" >&2
      exit 1
    fi

    # A `GLIBCXX` or `CXXABI` reference means a symbol still arrives from the
    # shared libstdc++, and the manylinux cap that this file exists to remove
    # would apply again.
    if readelf --dyn-syms --wide ${soname} | grep -qE '@(GLIBCXX|CXXABI)_'; then
      echo "cxx-runtime: ${soname} still references a versioned C++ symbol" >&2
      readelf --dyn-syms --wide ${soname} | grep -E '@(GLIBCXX|CXXABI)_' >&2
      exit 1
    fi

    # Every relocation has to resolve. A missing `-lm` is invisible until the
    # first `dlopen`, and this catches it in the build that made the library.
    if ldd -r ${soname} 2>&1 | grep -q 'undefined symbol'; then
      echo "cxx-runtime: ${soname} has an undefined symbol" >&2
      ldd -r ${soname} 2>&1 | grep 'undefined symbol' >&2
      exit 1
    fi

    runHook postCheck
  '';

  installPhase = ''
    runHook preInstall

    install -Dm555 -t "$out/lib" ${soname}
    # `-lnanopynixcxx` needs the unversioned name at link time.
    ln -s ${soname} "$out/lib/${lib.removeSuffix ".1" soname}"

    runHook postInstall
  '';

  passthru = {
    inherit soname;
    # The flags that make a consumer take this runtime instead of the shared
    # libstdc++. `nix/cxx-stdenv.nix` writes them into the `libcxx-ldflags` file
    # of a wrapped compiler, which the cc-wrapper of nixpkgs reads into
    # `NIX_CXXSTDLIB_LINK`.
    linkFlags = "-nostdlib++ -lnanopynixcxx";
  };

  meta = {
    description = "libstdc++ of gcc, as one shared library with a private soname";
    # **The library, and not the project.** nixpkgs records `GPL-3.0-or-later`
    # for gcc, which is the compiler. `COPYING.RUNTIME` of the same source adds
    # the GCC Runtime Library Exception to libstdc++, and that exception is what
    # lets a product that links this library carry its own terms. nixpkgs has no
    # attribute for the exception, so it is written out here.
    license = [
      lib.licenses.gpl3Plus
      {
        spdxId = "GCC-exception-3.1";
        fullName = "GCC Runtime Library Exception v3.1";
        url = "https://www.gnu.org/licenses/gcc-exception-3.1.html";
        free = true;
      }
    ];
    platforms = lib.platforms.linux;
  };
}
