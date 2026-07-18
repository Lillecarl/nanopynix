{
  lib,
  buildPythonPackage,
  pkg-config,
  python,
  nanobind,
  nix-util,
  nix-store,
  nix-expr,
  nix-cmd,
  nix-fetchers,
  nix-flake,
  nix-main,
  cmake,
  ninja,
  renderPyproject,
  version,
  enableTsan ? false,
  tsanRuntime ? null,
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
  attrs = renderPyproject {
    projectRoot = ./.;
    inherit python;
  };
in
buildPythonPackage (
  attrs
  // {
    version = "${attrs.version}-${version}";
    
    src = lib.cleanSource ./.;

    build-system = attrs.build-system ++ [
      cmake
      ninja
    ];

    nativeBuildInputs = [
      pkg-config
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
          nix-cmd
          nix-fetchers
          nix-flake
          nix-main
        ];
      in
      lib.unique ([ nanobind2_13 ] ++ recdep ++ (recursivePropagation recdep));

    dontUseCmakeConfigure = true;

    cmakeFlags = [
      "-Dnanobind_ROOT=${nanobind2_13}/${python.sitePackages}/nanobind/cmake"
      "-DPython_EXECUTABLE=${python}/bin/python"
    ]
    ++ lib.optionals enableTsan [
      # Each cmakeFlags entry becomes its own -Ccmake.args= token; a single
      # entry with embedded spaces gets re-split upstream into bare (invalid)
      # CMake arguments, so keep every flag space-free. RelWithDebInfo already
      # implies -g; frame-pointer retention isn't essential for TSAN's
      # DWARF-based unwinding.
      "-DCMAKE_CXX_FLAGS=-fsanitize=thread"
      "-DCMAKE_EXE_LINKER_FLAGS=-fsanitize=thread"
      "-DCMAKE_SHARED_LINKER_FLAGS=-fsanitize=thread"
      "-DCMAKE_BUILD_TYPE=RelWithDebInfo"
    ];

    dontStrip = enableTsan;

    # postInstall's stubgen and the pythonImportsCheck phase both dlopen()
    # these .so's into a fresh, plain python process; TSAN's runtime must be
    # preloaded before that process does anything else, or its static-TLS
    # setup fails ("cannot allocate memory in static TLS block") since a
    # late dlopen() can't grow the TLS block CPython already sized at
    # startup. Setting it derivation-wide covers both phases uniformly.
    env = lib.optionalAttrs (enableTsan && tsanRuntime != null) {
      LD_PRELOAD = tsanRuntime;
    };

    postInstall = ''
      _site="$out/${python.sitePackages}"
      for mod in nanopynix_util nanopynix_store nanopynix_expr nanopynix_fetchers nanopynix_flake nanopynix_main; do
        _pat=""
        if [ -f "src/$mod.pat" ]; then
          _pat="-p src/$mod.pat"
        fi
        PYTHONPATH="$_site:$PYTHONPATH" \
          ${python}/bin/python -m nanobind.stubgen -m "$mod" $_pat -O "$_site"
      done
      touch "$_site/py.typed"
    '';

    pythonImportsCheck = [
      "nanopynix_util"
      "nanopynix_store"
      "nanopynix_expr"
      "nanopynix_fetchers"
      "nanopynix_flake"
      "nanopynix_main"
    ];

    meta = attrs.meta // {
      license = lib.licenses.lgpl21Plus;
      platforms = lib.platforms.unix;
    };
  }
)
