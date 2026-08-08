# A stdenv that compiles with `zig cc`, against an old glibc.
#
# nixpkgs already carries `pkgs.zig.passthru.stdenv`. That stdenv targets the
# glibc of nixpkgs, so a binary that it builds needs `GLIBC_2.38` or later. See
# issue #111 for the measurement and for the chain that sets that floor.
#
# This stdenv differs in two ways:
#
#   - It passes `-target x86_64-linux-gnu.<version>`. zig carries a stub
#     library for each glibc version, so it builds no glibc and it refuses a
#     function that the target version does not have.
#   - It gives the wrappers no libc. `libc = null` stops cc-wrapper adding
#     `-idirafter <glibc-dev>/include` and stops bintools-wrapper adding
#     `-L<glibc>/lib`. zig supplies both.
#
# zig links libc++ statically **into every shared object**, so a process that
# loads five of them holds five C++ runtimes. That is a defect and not a size
# problem, because one library then destroys a static object of another. So
# this stdenv links one shared runtime instead, and `nix/zig-cxx-runtime.nix`
# gives the measurement and the mechanism. A build product still needs no C++
# runtime of the host: it carries this one.
#
# A build still has to *run* what it compiles, and zig names a loader that no
# NixOS machine has. `zig-cc-wrapper.sh` corrects each executable after the
# link, and the comment at the top of that file gives the whole reason.
{
  lib,
  zig,
  llvmPackages,
  patchelf,
  runCommand,
  runtimeShell,
  wrapCCWith,
  wrapBintoolsWith,
  overrideCC,
  stdenv,
  callPackage,
  # The oldest glibc that a build product must run against. This is the
  # `manylinux_2_34` tag, which covers RHEL 9, Debian 12 and Ubuntu 22.04.
  #
  # 2.28 covers more, and Nix does not build against it. `close_range` arrived
  # in glibc 2.34, and `SYS_close_range` arrived with it. Nix calls the first
  # one when the meson check finds it, and the raw syscall when the check does
  # not, so a target below 2.34 fails on both spellings at once. Measured with
  # zig 0.16: 2.28 and 2.31 fail, 2.34 and 2.35 compile.
  glibcVersion ? "2.34",
}:
let
  target = "${stdenv.hostPlatform.qemuArch}-linux-gnu.${glibcVersion}";

  # The one C++ runtime of this closure. `nix/zig-cxx-runtime.nix` gives the
  # whole reason, and issue #112 holds the measurement that made it necessary.
  cxxRuntime = callPackage ./zig-cxx-runtime.nix { inherit target; };

  hostLibc = lib.getLib stdenv.cc.libc;
  hostLoader = stdenv.cc.bintools.dynamicLinker;

  # The same shape as `pkgs.zig.cc-unwrapped`, plus the loader correction.
  cc-unwrapped =
    runCommand "zig-cc-${zig.version}"
      {
        pname = "zig-cc";
        inherit (zig) version;
        passthru = {
          isZig = true;
          targetPrefix = "";
        };
        meta = zig.meta // {
          mainProgram = "clang";
        };
      }
      ''
        mkdir -p $out/bin
        for tool in cc c++; do
          substitute ${./zig-cc-wrapper.sh} "$out/bin/$tool" \
            --subst-var-by shell ${runtimeShell} \
            --subst-var-by zig ${zig}/bin/zig \
            --subst-var-by tool "$tool" \
            --subst-var-by patchelf ${patchelf}/bin/patchelf \
            --subst-var-by loader ${hostLoader} \
            --subst-var-by libcLib ${hostLibc}/lib
          chmod +x "$out/bin/$tool"
        done
        ln -s $out/bin/cc $out/bin/clang
        ln -s $out/bin/c++ $out/bin/clang++
      '';

  # `zig.bintools-unwrapped` carries `ar`, `objcopy`, `ranlib` and `ld.lld`, and
  # nothing else. It has no `nm`, and libtool needs one: it asks `command to
  # parse nm output from clang object`, gets `failed`, and then writes a
  # `libtool` script that stops with `syntax error near unexpected token '|'`.
  # libssh2 and nghttp2 both stop there.
  #
  # The bintools of LLVM carry the whole set, and they are the right match for
  # a clang and lld toolchain. `zig ar` is `llvm-ar` already.
  bintools = wrapBintoolsWith {
    bintools = llvmPackages.bintools-unwrapped;
    libc = null;
  };

  cc = wrapCCWith {
    cc = cc-unwrapped;
    inherit bintools;
    libc = null;
    extraPackages = [ ];
    nixSupport.cc-cflags = [
      "-target"
      target
      # `zig cc` maps `-O2` and `-O3` onto its own ReleaseFast mode, and that
      # mode defines `NDEBUG`. The stdenv of nixpkgs never defines `NDEBUG`,
      # and Nix stops the build with `#error` when something else does. So this
      # flag restores the rule that the build system, and not the compiler,
      # decides whether an assertion stays. zig puts its own define at the
      # front of the command line, so a `-U` anywhere after it wins.
      "-UNDEBUG"

      # **Do not put `-fvisibility-ms-compat` here.** It corrects the type
      # information of the extension, and `nanopynix-bindings/package.nix`
      # gives it to that build alone. The reason it cannot go on the whole
      # closure is measured:
      #
      # The flag sets the default visibility of a *value* to hidden and of a
      # *type* to default. A package that already passes `-fvisibility=hidden`
      # therefore keeps its values as they were and gets its types back, which
      # is the correction. Every other package of this closure exports its API
      # by default visibility, and the flag hides that API. `attr` was the
      # first to stop:
      #
      #   ld.lld: error: undefined symbol: attr_get
      #   >>> referenced by attr.c:199, tools/attr.o:(main)
      #
      # `libattr.so` built, and the `attr` tool beside it could no longer link
      # against it.
    ];
    # **The C++ runtime is shared, and `-nostdlib++` belongs to the link
    # alone.**
    #
    # cc-wrapper reads this file into `NIX_CXXSTDLIB_LINK`, and it adds that
    # variable when it links C++. `libcxx-cxxflags` is the compile-time
    # partner, and it stays empty on purpose: `-nostdlib++` on a *compile*
    # takes away the include path of libc++ and all 18 of the `-D_LIBCPP_*`
    # macros that zig sets, and the compile then stops at
    # `'sstream' file not found`. Restoring those by hand would make this
    # closure disagree with the archives that the runtime holds, which is the
    # class of defect the runtime exists to remove. So a compile keeps every
    # flag that zig gives it, and only the link changes.
    nixSupport.libcxx-ldflags = [
      "-nostdlib++"
      "-L${cxxRuntime}/lib"
      "-lnanopynixcxx"
      # **The unwinder, for the consumer and not only for the runtime.** A
      # function with a cleanup emits a call to `_Unwind_Resume` in the object
      # that holds it, so every library of this closure names that symbol
      # itself. `-nostdlib++` takes away the `libunwind.a` that used to answer,
      # and the shared runtime must not answer either: `libgcc_s.so.1` owns
      # unwinding for the process, and two unwinders in one process end a
      # `throw` with SIGSEGV. `nix/zig-cxx-runtime.nix` gives that backtrace.
      #
      # This is a link input, and nothing of the path survives the link. The
      # link records the soname, `manylinux_2_34` whitelists that soname, and
      # the host supplies the file.
      #
      # **A stub, named as a file, and never `-lgcc_s`.** zig answers that flag
      # with its own static libunwind, which is the defect again and links with
      # no complaint, and the real `libgcc_s.so.1` of gcc 15 names a glibc
      # symbol above the floor of this closure. `nix/zig-cxx-runtime.nix` holds
      # both measurements, and it builds the stub.
      "${cxxRuntime}/${cxxRuntime.unwinderStub}"
    ];
    nixSupport.cc-ldflags = [
      # lld 17 made `--no-undefined-version` its default, and GNU ld only
      # warns. A version script that names a symbol which the build did not
      # produce is therefore an error here and not on the stdenv of nixpkgs.
      # onetbb is the first package of the closure to carry such a script.
      # Every package here links with lld, so the whole closure needs the older
      # rule, and not one package.
      "--undefined-version"
    ];
  };
in
# `cxxRuntime` rides in the wheel like every other shared library, so
# `nix/wheel-licenses.nix` has to name it. It is reachable here alone, because
# `target` decides it and `target` is computed above.
(overrideCC stdenv cc) // { inherit cxxRuntime; }
