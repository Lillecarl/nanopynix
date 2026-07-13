{
  lib,
  buildPythonPackage,
  pkg-config,
  python,
  nanobind,
  nix,
  cmake,
  ninja,
  scikit-build-core,
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

buildPythonPackage {
  pname = "nanopynix-bindings";
  version = "0.1.0";
  pyproject = true;

  src = lib.cleanSource ./.;

  build-system = [
    cmake
    ninja
    scikit-build-core
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
  ];

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

  meta = with lib; {
    description = "nanobind-based Python bindings for Nix (compiled extensions)";
    license = licenses.lgpl21Plus;
    platforms = platforms.unix;
  };
}
