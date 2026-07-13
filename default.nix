{
  inputs ? (import ./nix/compat.nix).inputs,
  system ? builtins.currentSystem,
  pkgs ? inputs.nixpkgs.legacyPackages.${system},
}:
let
  inherit (pkgs) python3Packages;

  grpclab-all = pkgs.callPackage inputs.grpclib-transports { };
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
  clypi = python3Packages.callPackage ./nix/clypi.nix { };
  pynix = python3Packages.callPackage ./pynix/package.nix { inherit nanopynix clypi; };

  shell = python3Packages.callPackage ./nix/shell.nix {
    inherit nanopynix pynix;
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
    clypi
    ;
  inherit (pkgs) lib;
}
