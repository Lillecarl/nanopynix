let
  flake = (import ./nix/compat.nix);
in
{
  inputs ? flake.inputs,
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
  nanopynixForNix =
    nix:
    let
      nanopynix-bindings = python3Packages.callPackage ./bindings/package.nix {
        inherit nix;
      };
    in
    python3Packages.callPackage ./python/package.nix {
      inherit
        nanopynix-bindings
        nanopynix-proto
        grpclib-transports
        ;
    };
  nixVersions = pkgs.lib.filterAttrs (
    _: nix:
    let
      hasLibs = builtins.tryEval (nix ? libs);
    in
    hasLibs.success && hasLibs.value
  ) pkgs.nixVersions;
  nanopynix-nixVersions = pkgs.lib.mapAttrs (_: nix: nanopynixForNix nix) nixVersions;
  clypi = python3Packages.callPackage ./nix/clypi.nix { };
  pynix = python3Packages.callPackage ./pynix/package.nix { inherit nanopynix clypi; };

  shell = python3Packages.callPackage ./nix/shell.nix {
    inherit nanopynix pynix;
  };
in
{
  inherit flake;
  inherit
    pkgs
    shell
    nanopynix
    nanopynix-nixVersions
    nanopynix-bindings
    nanopynix-proto
    pynix
    grpclib-transports
    clypi
    ;
  inherit (pkgs) lib;
}
