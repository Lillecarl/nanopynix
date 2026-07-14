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
    ++ [
      nanopynix
      pynix
      pytest
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
