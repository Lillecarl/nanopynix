{
  lib,
  buildPythonPackage,
  nanopynix,
  python,
  renderPyproject,
}:
let
  attrs = renderPyproject {
    projectRoot = lib.cleanSource ./.;
    inherit python;
    pythonPackages = python.pkgs // {
      inherit nanopynix;
      "tree-sitter-nix" = python.pkgs.tree-sitter-grammars.tree-sitter-nix.overridePythonAttrs (_: {
        pname = "tree-sitter-nix";
      });
    };
  };
in
buildPythonPackage (
  attrs
  // {
    pythonImportsCheck = [
      "nanopynix_helpers"
    ];

    meta = attrs.meta // {
      platforms = lib.platforms.unix;
    };
  }
)
