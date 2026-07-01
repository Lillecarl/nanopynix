{
  lib,
  mkShell,
  just,
  pyright,
  ruff,
  pyupgrade,
  sqlite,
  python3,
  pynixd,
  nix,
  lix,
}:
let
  python = python3.withPackages (
    ps:
    pynixd.dependencies
    ++ [
      ps.pytest
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
    export PYTHONPATH="$PWD:${python}/${python.sitePackages}:$PYTHONPATH"
    export LIX_BIN=${lib.getExe lix}
    export NIX_BIN=${lib.getExe nix}
  '';
}
