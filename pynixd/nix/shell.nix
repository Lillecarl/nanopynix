{
  lib,
  mkShell,
  just,
  pyright,
  ruff,
  pyupgrade,
  sqlite,
  pynixd,
  nix,
  nanopython,
}:
let
  python = nanopython.withPackages (
    ps:
    pynixd.dependencies
    ++ [
      ps.pytest
      ps.sphinx
      ps.myst-parser
      ps.furo
    ]
  );
in
mkShell {
  packages = [
    python
    just
    pyright
    ruff
    pyupgrade
    sqlite
  ];
  shellHook = ''
    export PYTHONPATH="$PWD:$PWD/nix-daemon-protocol/src:${python}/${python.sitePackages}:$PYTHONPATH"
    export NIX_BIN=${lib.getExe nix}
  '';
}
