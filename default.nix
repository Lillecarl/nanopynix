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
  mkNanopynixBindings = nix: python3Packages.callPackage ./bindings/package.nix { inherit nix; };
  mkNanopynix =
    nix:
    python3Packages.callPackage ./python/package.nix {
      nanopynix-bindings = mkNanopynixBindings nix;
      inherit
        nanopynix-proto
        grpclib-transports
        ;
    };
  mkPynix = nanopynix: python3Packages.callPackage ./pynix/package.nix { inherit nanopynix clypi; };
  mkNanopynixTests =
    {
      nix,
      nanopynix,
    }:
    python3Packages.callPackage ./nix/tests.nix {
      inherit clypi nix nanopynix;
      flakeCompatishSrc = inputs.flake-compatish;
      grpclibTransportsSrc = inputs.grpclib-transports;
      pynix = mkPynix nanopynix;
    };
  withTests =
    {
      nix,
      nanopynix,
    }:
    nanopynix.overrideAttrs (old: {
      passthru = old.passthru or { } // {
        tests = mkNanopynixTests { inherit nix nanopynix; };
      };
    });
  nanopynix = withTests {
    nix = pkgs.nix;
    nanopynix = mkNanopynix pkgs.nix;
  };
  nanopynix-bindings = mkNanopynixBindings pkgs.nix;
  nanopynixForNix =
    nix:
    withTests {
      nanopynix = mkNanopynix nix;
      inherit nix;
    };
  nixVersions = pkgs.lib.filterAttrs (
    _: nix:
    let
      hasLibs = builtins.tryEval (nix ? libs);
    in
    hasLibs.success && hasLibs.value
  ) pkgs.nixVersions;
  nanopynix-nixVersions = pkgs.lib.mapAttrs (_: nix: nanopynixForNix nix) nixVersions;
  nanopynix-nixVersions-tests = pkgs.callPackage ./nix/nix-version-tests.nix {
    inherit nanopynix-nixVersions;
    inherit (inputs) nixpkgs;
  };
  clypi = python3Packages.callPackage ./nix/clypi.nix { };
  pynix = mkPynix nanopynix;

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
    nanopynix-nixVersions-tests
    nanopynix-bindings
    nanopynix-proto
    pynix
    grpclib-transports
    clypi
    ;
  inherit (pkgs) lib;
}
