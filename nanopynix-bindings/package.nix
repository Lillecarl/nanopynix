{
  lib,
  buildPythonPackage,
  pkg-config,
  python,
  nanobind,
  nix-util,
  nix-store,
  nix-expr,
  nix-fetchers,
  nix-flake,
  cmake,
  ninja,
  pyproject-nix,
  pyprojectVersionPatchHook,
  version,
  sanitizer ? null,
  sanitizerRuntime ? null,
}:

let
  # Use latest nanobind (2.13.0 26/07/09) because it's fixed a stub generation bug
  nanobind2_13 = nanobind.overrideAttrs (
    final: prev: {
      version = "2.13.0";
      src = prev.src.override {
        hash = "sha256-YAqjcVBkuNsXvrAaVmDRLQ1F38UBqdnIf8+OseNBzG4=";
      };
    }
  );
in
let
  attrs =
    (pyproject-nix.lib.project.loadPyproject { projectRoot = toString ./.; })
    .renderers.buildPythonPackage
      { inherit python; };
in
buildPythonPackage (
  attrs
  // {
    # PEP 440 local version identifier: `<our version>+nix<nix version>`, so
    # two builds of the same source against different Nix components stay
    # distinguishable *and* parse. The old `-` form did not -- `0.1.0-2.35.1`
    # is not a version at all under PEP 440 (a `-` separator is only legal
    # before a post/pre/dev segment, so `0.1.0-2` parses as `0.1.0.post2` and
    # `0.1.0-2.35.1` is rejected outright). That went unnoticed until nixpkgs
    # added pythonMetadataCheckHook, which parses both the derivation version
    # and the one in `.dist-info/METADATA` with `packaging`.
    #
    # The local segment is the right place for it: it keeps the public
    # version `0.1.0` (so dependency specifiers on this project still work),
    # it orders the way you would want between Nix versions, and PEP 440
    # normalisation accepts the git scope's version too
    # (`2.35pre20260619_f8bb823a` -> local `nix2.35pre20260619.f8bb823a`).
    version = "${attrs.version}+nix${version}";

    src = ./.;

    build-system = attrs.build-system ++ [
      cmake
      ninja
    ];

    nativeBuildInputs = [
      pkg-config
      # Rewrites `project.version` in pyproject.toml to the derivation's
      # `version` before the build reads it, so the wheel's METADATA carries
      # the `+nix<version>` local segment too. Without it the build is still
      # correct but pythonMetadataCheckHook fails the derivation, because
      # scikit-build-core would emit the unsuffixed `0.1.0` from the checkout
      # while the derivation claims the suffixed one.
      pyprojectVersionPatchHook
    ];
    # nix's modular components don't propagatedBuildInputs their own C
    # library deps (blake3, boost, libarchive, libsodium, ...) to consumers,
    # so pkg-config can't find e.g. libblake3.pc for nix-util unless it's
    # walked out of nix-util's own buildInputs here, recursively (a
    # transitive dep can itself pull in another).
    buildInputs =
      let
        recursivePropagation =
          derivations:
          lib.concatMap (
            x:
            if x.buildInputs or null != null then
              [ x ] ++ x.buildInputs ++ recursivePropagation x.buildInputs
            else
              [ ]
          ) derivations;
        recdep = [
          nix-util
          nix-store
          nix-expr
          nix-fetchers
          nix-flake
        ];
      in
      lib.unique ([ nanobind2_13 ] ++ recdep ++ (recursivePropagation recdep));

    dontUseCmakeConfigure = true;

    cmakeFlags = [
      "-Dnanobind_ROOT=${nanobind2_13}/${python.sitePackages}/nanobind/cmake"
      "-DPython_EXECUTABLE=${python}/bin/python"
    ]
    ++ lib.optionals (sanitizer != null) [
      # Each cmakeFlags entry becomes its own -Ccmake.args= token, and a single
      # entry with embedded spaces gets re-split upstream into bare (invalid)
      # CMake arguments -- so only a space-free flag can go here. That rules
      # out CMAKE_CXX_FLAGS, which needs the whole space-separated set: it is a
      # plain string variable, so joining with ";" would put a literal
      # semicolon on the compile line. The compile flags go through
      # NIX_CFLAGS_COMPILE below instead, which is a string and takes spaces.
      #
      # RelWithDebInfo already implies -g; frame-pointer retention isn't
      # essential for the DWARF-based unwinding either sanitizer does.
      "-DCMAKE_EXE_LINKER_FLAGS=${sanitizer.linkFlag}"
      "-DCMAKE_SHARED_LINKER_FLAGS=${sanitizer.linkFlag}"
      "-DCMAKE_BUILD_TYPE=RelWithDebInfo"
    ];

    dontStrip = sanitizer != null;

    # postInstall's stubgen and the pythonImportsCheck phase both dlopen()
    # these .so's into a fresh, plain python process; the sanitizer runtime
    # must be preloaded before that process does anything else, or its
    # static-TLS setup fails ("cannot allocate memory in static TLS block")
    # since a late dlopen() can't grow the TLS block CPython already sized at
    # startup. Setting it derivation-wide covers both phases uniformly.
    #
    # `sanitizerRuntime` is null for the UBSan variant, so neither phase gets a
    # preload there. That variant needs none: the link of the extension records
    # its runtime as a dependency, and the loader brings the runtime in.
    # nix/sanitizer.nix gives the whole reason.
    #
    # **A preload that reaches every phase also reaches `bash`.** The ASAN
    # variant needs `ASAN_OPTIONS=detect_leaks=0` for that reason alone, or
    # LeakSanitizer reports the build shell and the build fails. That value
    # comes from `sanitizer.buildEnv`, which gives the measurement.
    env =
      lib.optionalAttrs (sanitizer != null) (
        {
          # The same flags every nix-* component gets (nix/sanitizer.nix), so the
          # extension and the libraries it calls agree about instrumentation.
          NIX_CFLAGS_COMPILE = sanitizer.flags;
        }
        // sanitizer.buildEnv
      )
      // lib.optionalAttrs (sanitizerRuntime != null) {
        LD_PRELOAD = sanitizerRuntime;
      };

    # One `.pyi` per area, exactly as when each area was its own extension
    # module -- so `nanopynix_bindings.expr` still resolves to `expr.pyi` for a
    # type checker even though there is no `expr` file on disk any more. A stub
    # with no source counterpart is fine in a `py.typed` package.
    #
    # `-o <file>` rather than `-O <dir>`: stubgen infers the output name from
    # the module's `__file__`, and a submodule of an extension has none. It
    # says so and exits rather than guessing, which is how this was found.
    postInstall = ''
      _site="$out/${python.sitePackages}"
      for mod in errors signals util store expr fetchers flake; do
        _pat=""
        if [ -f "src/$mod.pat" ]; then
          _pat="-p src/$mod.pat"
        fi
        PYTHONPATH="$_site:$PYTHONPATH" \
          ${python}/bin/python -m nanobind.stubgen -m "nanopynix_bindings.$mod" $_pat -o "$_site/nanopynix_bindings/$mod.pyi"
      done
      touch "$_site/nanopynix_bindings/py.typed"
    '';

    pythonImportsCheck = [
      "nanopynix_bindings.errors"
      "nanopynix_bindings.signals"
      "nanopynix_bindings.util"
      "nanopynix_bindings.store"
      "nanopynix_bindings.expr"
      "nanopynix_bindings.fetchers"
      "nanopynix_bindings.flake"
    ];

    meta = attrs.meta // {
      license = lib.licenses.lgpl21Plus;
      platforms = lib.platforms.unix;
    };
  }
)
