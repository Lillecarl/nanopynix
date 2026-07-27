{
  lib,
  buildPythonPackage,
  python,
  renderPyproject,
}:
let
  attrs = renderPyproject {
    projectRoot = toString ./.;
    inherit python;
  };
in
buildPythonPackage (
  attrs
  // {
    src = ./.;

    pythonImportsCheck = [
      "nanopynix_helpers"
    ];

    meta = attrs.meta // {
      platforms = lib.platforms.unix;
    };
  }
)
