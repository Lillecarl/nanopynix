{
  pkgs ? import <nixpkgs> { },
}:
let
  inherit (pkgs) python3Packages;

  grpclab-all = pkgs.callPackage ../grpclab { };
  inherit (grpclab-all)
    grpclib-transports
    betterproto2
    betterproto2-compiler
    ;

  nanopynix-proto = python3Packages.callPackage ./proto/package.nix {
    inherit betterproto2 betterproto2-compiler;
  };
  nanopynix-bindings = python3Packages.callPackage ./bindings/package.nix { };
  nanopynix = python3Packages.callPackage ./python/package.nix {
    inherit
      nanopynix-bindings
      nanopynix-proto
      grpclib-transports
      ;
  };
  pynix = python3Packages.callPackage ./pynix/package.nix { inherit nanopynix; };

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

  inherit
    pkgs
    python
    nanopynix
    nanopynix-bindings
    nanopynix-proto
    pynix
    grpclib-transports
    ;
  inherit (pkgs) lib;
}
