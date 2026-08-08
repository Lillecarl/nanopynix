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
# zig also links libc++ statically, so a library that this stdenv builds needs
# no C++ runtime on the host.
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
overrideCC stdenv cc
