let
  flake = (import ./nix/compat.nix);
in
{
  inputs ? flake.inputs,
  system ? builtins.currentSystem,
  pkgs ? inputs.nixpkgs.legacyPackages.${system},
}:
let
  inherit (pkgs) lib python3Packages;

  inherit (pkgs.callPackage inputs.grpclib-transports { })
    grpclib-transports
    betterproto2
    betterproto2-compiler
    ;

  nanopynix-proto = python3Packages.callPackage ./proto/package.nix {
    inherit betterproto2 betterproto2-compiler;
  };

  clypi = python3Packages.callPackage ./nix/clypi.nix { };

  nanopynixForNix =
    nix:
    lib.makeScope
      (
        extra:
        lib.callPackageWith (
          pkgs
          // python3Packages
          // {
            inherit
              nanopynix-proto
              grpclib-transports
              clypi
              ;
          }
          // extra
        )
      )
      (self: {
        inherit nix;
        nanopynix-bindings = self.callPackage ./bindings/package.nix { };
        nanopynix = self.callPackage ./python/package.nix { };
        pynix = self.callPackage ./pynix/package.nix { };
        tests = self.callPackage ./nix/tests.nix {
          inherit (inputs) nixpkgs;
        };
      });

  nanopynixVersions = lib.pipe pkgs.nixVersions [
    (lib.filterAttrs (
      _: nix:
      let
        canEval = builtins.tryEval nix;
      in
      canEval.success && lib.isDerivation canEval.value
    ))
    (lib.mapAttrs (_: nix: nanopynixForNix nix))
  ];

  nanopynix-all-tests = pkgs.callPackage ./nix/nix-version-tests.nix {
    nanopynixVersions = nanopynixVersions;
    inherit (inputs) nixpkgs;
  };

  shell = python3Packages.callPackage ./nix/shell.nix {
    inherit (nanopynixVersions.stable) nanopynix pynix;
  };
in
{
  inherit (pkgs) lib;

  inherit (nanopynixVersions.stable)
    nanopynix
    nanopynix-bindings
    pynix
    tests
    ;

  inherit
    flake
    pkgs
    shell
    nanopynixVersions
    nanopynix-all-tests
    nanopynix-proto
    clypi
    grpclib-transports
    ;
}
