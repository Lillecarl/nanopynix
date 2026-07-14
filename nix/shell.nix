{
  mkShell,
  python,
  pyright,
  ruff,
  nixfmt,
  clang-tools,
  taplo,
  treefmt,
  nanopynix,
  pynix,
  pytest,
  pytest-dependency,
  anyio,
  sphinx,
  myst-parser,
  furo,
}:
let
  pythonEnv = python.withPackages (
    pp:
    nanopynix.dependencies
    ++ nanopynix.nativeBuildInputs
    ++ nanopynix.passthru.testInputs
    ++ pynix.dependencies
    ++ pynix.nativeBuildInputs
    ++ [
      nanopynix
      pynix
      pytest
      pytest-dependency
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
    nixfmt
    clang-tools
    taplo
    treefmt
  ];
}
