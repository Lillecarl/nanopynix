{
  lib,
  buildPythonPackage,
  pkg-config,
  python,
  nanobind,
  nix,
  cmake,
  ninja,
  renderPyproject,
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

    src = lib.cleanSource ./.;

    build-system = attrs.build-system ++ [
      cmake
      ninja
    ];

    nativeBuildInputs = [
      pkg-config
    ];
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
      in
      [
        nanobind2_13
      ]
      ++ lib.pipe nix.libs [
        lib.attrsToList
        (lib.map ({ value, ... }: value))
        recursivePropagation
        lib.unique
      ];

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
