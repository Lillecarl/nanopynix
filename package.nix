{
  lib,
  buildPythonPackage,
  pkg-config,
  pydantic,
  pyyaml,
  python,
  nanobind,
  janus,
  nix,
  cmake,
  ninja,
  scikit-build-core,
}:

buildPythonPackage {
  pname = "nanopynix";
  version = "0.1.0";
  pyproject = true;

  src = lib.cleanSourceWith {
    filter =
      path: type:
      let
        relPath = lib.removePrefix (toString ./. + "/") (toString path);
        baseName = lib.baseNameOf path;
      in
      lib.cleanSourceFilter path type && baseName != "tests";
    src = ./.;
  };

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
      nanobind
    ]
    ++ lib.pipe nix.libs [
      lib.attrsToList
      (lib.map ({ value, ... }: value))
      recursivePropagation
      lib.unique
    ];
  dependencies = [
    janus
    pydantic
    pyyaml
  ];

  dontUseCmakeConfigure = true;

  cmakeFlags = [
    "-Dnanobind_ROOT=${nanobind}/${python.sitePackages}/nanobind/cmake"
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
  '';

  pythonImportsCheck = [
    "nanopynix_util"
    "nanopynix_store"
    "nanopynix_expr"
    "nanopynix_fetchers"
    "nanopynix_flake"
    "nanopynix_main"
    "nanopynix"
  ];

  meta = with lib; {
    description = "nanobind-based Python bindings for Nix";
    license = licenses.lgpl21Plus;
    platforms = platforms.unix;
  };
}
