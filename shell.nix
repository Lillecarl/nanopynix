{
  pkgs ? import <nixpkgs> { },
}:
let
  inherit (pkgs) lib;
  default = import ./. { inherit pkgs; };
  python = pkgs.python3.withPackages (
    ps:
    default.package.dependencies
    ++ [
      ps.pytest
      ps.pytest-asyncio
      ps.pytest-timeout
    ]
  );
in
pkgs.mkShell {
  packages = [
    python
    pkgs.just
    pkgs.nix
    pkgs.pyright
    pkgs.ruff
  ];
  shellHook = ''
    export PYNIXD_TEST_NIX=${./test.nix}
    export PYTHONPATH="$PWD:${python}/${python.sitePackages}:$PYTHONPATH"
    export LIX_BIN=${lib.getExe pkgs.lix}
    export NIX_BIN=${lib.getExe pkgs.nix}
  '';
}
