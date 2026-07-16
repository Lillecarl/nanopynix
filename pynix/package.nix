{
  lib,
  buildPythonApplication,
  nanopynix,
  clypi,
  python,
  renderPyproject,
}:
let
  attrs = renderPyproject {
    projectRoot = ./.;
    inherit python;
    pythonPackages = python.pkgs // {
      inherit nanopynix clypi;
      "tree-sitter-nix" = python.pkgs.tree-sitter-grammars.tree-sitter-nix.overridePythonAttrs (_: {
        pname = "tree-sitter-nix";
      });
    };
  };
in
buildPythonApplication (
  attrs
  // {

    src = lib.cleanSource ./.;

    meta = attrs.meta // {
      platforms = lib.platforms.unix;
    };
  }
)
