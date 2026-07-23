{
  lib,
  buildPythonPackage,
  nanopynix,
  tree-sitter-nix,
  python,
  renderPyproject,
}:
let
  attrs = renderPyproject {
    projectRoot = toString ./.;
    inherit python;
    pythonPackages = python.pkgs // {
      inherit nanopynix tree-sitter-nix;
    };
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
