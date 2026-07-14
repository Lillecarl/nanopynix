{
  lib,
  callPackage,
  # build
  buildPythonPackage,
  hatchling,
  # nixpkgs deps
  janus,
  pydantic,
  pydantic-settings,
  pyyaml,
  strip-ansi,
  # nixpkgs test deps
  pytest-dependency,
  anyio,
  # cool deps
  nanopynix-bindings ? callPackage ../bindings/package.nix { },
  nanopynix-proto ? callPackage ../proto/package.nix { },
  grpclib-transports,
}:
buildPythonPackage (
  finalAttrs:
  let
    nativeCheckInputs = [
      pytest-dependency
      anyio
    ];
  in
  {
    pname = "nanopynix";
    version = "0.1.0";
    pyproject = true;

    src = lib.cleanSource ./.;

    build-system = [
      hatchling
    ];

    dependencies = [
      nanopynix-bindings
      nanopynix-proto
      grpclib-transports
      janus
      pydantic
      pydantic-settings
      pyyaml
      strip-ansi
    ];
    inherit nativeCheckInputs;

    pythonImportsCheck = [
      "nanopynix"
    ];

    meta = with lib; {
      description = "nanobind-based Python bindings for Nix";
      license = licenses.lgpl21Plus;
      platforms = platforms.unix;
    };
    passthru.testInputs = nativeCheckInputs;
  }
)
