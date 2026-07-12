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

  shell = python3Packages.callPackage ./nix/shell.nix {
    inherit nanopynix;
  };
in
{
  inherit
    pkgs
    shell
    nanopynix
    nanopynix-bindings
    nanopynix-proto
    pynix
    grpclib-transports
    ;
  inherit (pkgs) lib;
}
