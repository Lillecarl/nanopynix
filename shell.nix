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
      ps.pytest-forked
    ]
  );
in
pkgs.mkShell {
  packages = [
    python
    pkgs.just
    pkgs.pyright
    pkgs.ruff
    pkgs.sqlite
  ];
  shellHook = ''
    export PYTHONPATH="$PWD:${python}/${python.sitePackages}:$PYTHONPATH"
    export LIX_BIN=${lib.getExe pkgs.lix}
    export NIX_BIN=${lib.getExe pkgs.nix}
    # export NIX_BIN=/home/lillecarl/Code/nix/build/src/nix/nix
  '';
}
