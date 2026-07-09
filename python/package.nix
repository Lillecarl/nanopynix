{
  lib,
  buildPythonPackage,
  hatchling,
  nanopynix-bindings,
  janus,
  pydantic,
  pyyaml,
  strip-ansi,
}:

buildPythonPackage {
  pname = "nanopynix";
  version = "0.1.0";
  pyproject = true;

  src = lib.cleanSourceWith {
    filter =
      path: type:
      let
        baseName = lib.baseNameOf path;
      in
      lib.cleanSourceFilter path type && baseName != "tests";
    src = ./.;
  };

  build-system = [
    hatchling
  ];

  dependencies = [
    nanopynix-bindings
    janus
    pydantic
    pyyaml
    strip-ansi
  ];

  pythonImportsCheck = [
    "nanopynix"
  ];

  meta = with lib; {
    description = "nanobind-based Python bindings for Nix";
    license = licenses.lgpl21Plus;
    platforms = platforms.unix;
  };
}
