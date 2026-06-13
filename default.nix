{
  pkgs ? import <nixpkgs> { },
}:
let
  inherit (pkgs) python3Packages;

  nanopynix = python3Packages.callPackage ./package.nix { };

  python = pkgs.python3.withPackages (pp: [
    nanopynix
    pp.pytest
    pp.pytest-asyncio
  ]);
in
{
  shell = pkgs.mkShell {
    packages = [
      python
      pkgs.pyright
      pkgs.ruff
    ];
  };
  ruff = pkgs.writeShellApplication {
    name = "ruff";
    runtimeInputs = [
      python
      pkgs.ruff
    ];
    text = # bash
      ''
        ruff "$@"
      '';
  };
  pyright = pkgs.writeShellApplication {
    name = "pyright";
    runtimeInputs = [
      python
      pkgs.pyright
    ];
    text = # bash
      ''
        pyright "$@"
      '';
  };
  pytest = pkgs.writeShellApplication {
    name = "pytest";
    runtimeInputs = [
      python
    ];
    text = # bash
      ''
        pytest "$@"
      '';
  };

  inherit pkgs python nanopynix;
  inherit (pkgs) lib;
}
