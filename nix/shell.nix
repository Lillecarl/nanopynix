{
  mkShell,
  python,
  pyright,
  ruff,
  nanopynix,
  pynix,
  pytest,
  anyio,
  sphinx,
  myst-parser,
  furo,
}:
let
  pythonEnv = python.withPackages (
    pp:
    nanopynix.dependencies ++ nanopynix.nativeBuildInputs ++ pynix.dependencies ++ pynix.nativeBuildInputs ++ [
      nanopynix
      pynix
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
