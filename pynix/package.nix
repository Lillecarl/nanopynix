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
