{
  lib,
  stdenvNoCC,
  python,
  pynix,
  sphinx,
  myst-parser,
  furo,
}:
let
  pythonEnv = python.withPackages (
    _:
    pynix.dependencies
    ++ [
      pynix
      sphinx
      myst-parser
      furo
    ]
  );
in
stdenvNoCC.mkDerivation {
  pname = "nanopynix-docs";
  version = "0";

  src = ../.;

  nativeBuildInputs = [ pythonEnv ];

  buildPhase = ''
    runHook preBuild
    NANOPYNIX_DOCS_OFFLINE=1 sphinx-build -b html docs "$out" -W
    runHook postBuild
  '';

  dontInstall = true;
}
