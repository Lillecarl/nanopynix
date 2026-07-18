{
  lib,
  stdenv,
}:
let
  # ThreadSanitizer's runtime must be loaded before any other allocation
  # happens in the process (LD_PRELOAD), or its shadow-memory setup silently
  # fails to intercept everything -- this bites us specifically because the
  # instrumented code is a native extension dlopen()'d into a plain,
  # non-instrumented CPython interpreter, not a from-scratch instrumented
  # executable.
  tsanRuntime = "${stdenv.cc.cc.lib}/lib/libtsan.so";

  tsanFlags = [
    "-fsanitize=thread"
    "-fno-omit-frame-pointer"
    "-g"
  ];
  tsanFlagsStr = toString tsanFlags;
in
{
  inherit tsanRuntime;

  # Applied via `nixComponents.overrideAllMesonComponents` so every nix-*
  # library (nix-util, nix-store, nix-expr, nix-fetchers, nix-cmd, ...) gets
  # consistent instrumentation -- a race straddling two of these libraries
  # would be invisible to TSAN if only one side were instrumented.
  mesonComponentOverrides = _finalAttrs: prevAttrs: {
    env = (prevAttrs.env or { }) // {
      NIX_CFLAGS_COMPILE = toString [
        (prevAttrs.env.NIX_CFLAGS_COMPILE or "")
        tsanFlagsStr
      ];
    };
    dontStrip = true;
    # "release"/"minsize" mesonBuildType auto-enables LTO (see nixpkgs'
    # nix modular packaging/components.nix), which both slows down TSAN's
    # instrumentation pass and makes reported stacks harder to read via
    # cross-TU inlining. "debugoptimized" keeps -O2 -g without LTO.
    mesonBuildType = "debugoptimized";
  };

  # sqlite is a plain buildInput of nix-store (from outside the nix
  # component scope), not one of the meson components above, so it needs
  # its own override -- the crash we're chasing is inside sqlite3_step
  # itself, so if the actual race touches sqlite's own internal state
  # rather than just nix's wrapper code, an uninstrumented sqlite would
  # hide it from TSAN entirely.
  sanitizeSqlite =
    sqlite:
    sqlite.overrideAttrs (old: {
      env = (old.env or { }) // {
        NIX_CFLAGS_COMPILE = toString [
          (old.env.NIX_CFLAGS_COMPILE or "")
          tsanFlagsStr
        ];
      };
      dontStrip = true;
      # sqlite's own test suite fails under TSAN instrumentation (unrelated
      # to the race we're hunting in nix's usage of it) -- skip it rather
      # than debug sqlite's own tests here.
      doCheck = false;
      doInstallCheck = false;
    });
}
