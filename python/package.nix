{
  lib,
  callPackage,
  buildPythonPackage,
  # cool deps
  nanopynix-bindings ? callPackage ../bindings/package.nix { },
  nanopynix-proto ? callPackage ../proto/package.nix { },
  grpclib-transports,
  python,
  renderPyproject,
}:
let
  attrs = renderPyproject {
    projectRoot = ./.;
    inherit python;
    pythonPackages = python.pkgs // {
      "nanopynix-bindings" = nanopynix-bindings;
      "nanopynix-proto" = nanopynix-proto;
      "grpclib-transports" = grpclib-transports;
    };
  };
in
buildPythonPackage (
  attrs
  // {

    src = lib.cleanSource ./.;

    pythonImportsCheck = [
      "nanopynix"
    ];

    meta = attrs.meta // {
      license = lib.licenses.lgpl21Plus;
      platforms = lib.platforms.unix;
    };
  }
)
