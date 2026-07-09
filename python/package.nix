{
  lib,
  callPackage,
  buildPythonPackage,
  hatchling,
  janus,
  pydantic,
  pyyaml,
  strip-ansi,
  nanopynix-bindings ? callPackage ../bindings/package.nix { },
}:

buildPythonPackage {
  pname = "nanopynix";
  version = "0.1.0";
  pyproject = true;

  src = lib.cleanSource ./.;

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
