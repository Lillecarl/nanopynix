{
  lib,
  buildPythonApplication,
  hatchling,
  nanopynix,
  clypi,
  rich,
  structlog,
}:

buildPythonApplication {
  pname = "pynix";
  version = "0.1.0";
  pyproject = true;

  src = lib.cleanSource ./.;

  build-system = [
    hatchling
  ];

  dependencies = [
    nanopynix
    clypi
    rich
    structlog
  ];

  meta = with lib; {
    description = "End-to-end harness that consumes nanopynix as a library dependency";
    platforms = platforms.unix;
  };
}
