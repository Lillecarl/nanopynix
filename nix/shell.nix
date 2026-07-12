{
  mkShell,
  python,
  pyright,
  ruff,
  nanopynix,
  pytest,
  anyio,
  sphinx,
  myst-parser,
  furo,
}:
let
  pythonEnv = python.withPackages (
    pp:
    nanopynix.dependencies ++ nanopynix.nativeBuildInputs ++ [
      nanopynix
      pytest
      anyio
      sphinx
      myst-parser
      furo
    ]
  );
in
mkShell {
  packages = [
    pythonEnv
    pyright
    ruff
  ];
}
