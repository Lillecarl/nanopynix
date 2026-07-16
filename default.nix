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

  pyproject-nix = import "${inputs.pyproject-nix}" { inherit lib; };

  renderPyproject =
    {
      projectRoot,
      python,
      pythonPackages ? python.pkgs,
    }:
    (pyproject-nix.lib.project.loadPyproject { inherit projectRoot; }).renderers.buildPythonPackage {
      inherit python pythonPackages;
    };

  renderEditablePyproject =
    {
      projectRoot,
      root,
      python,
      pythonPackages ? python.pkgs,
      extras ? [ ],
    }:
    (pyproject-nix.lib.project.loadPyproject { inherit projectRoot; }).renderers.mkPythonEditablePackage
      {
        inherit
          root
          python
          pythonPackages
          extras
          ;
      };

  inherit (pkgs.callPackage inputs.grpclib-transports { })
    grpclib-transports
    betterproto2
    betterproto2-compiler
    ;

  nanopynix-proto = python3Packages.callPackage ./proto/package.nix {
    inherit betterproto2 betterproto2-compiler renderPyproject;
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
              pyproject-nix
              renderPyproject
              renderEditablePyproject
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
        shell = self.callPackage ./nix/shell.nix { };
        tests = self.callPackage ./nix/tests.nix {
          inherit (inputs) nixpkgs;
        };
        nanopynix-docs = self.callPackage ./nix/docs.nix { };
      });

  dedupeVersions = pkgs.callPackage ./nix/dedupe-nix-versions.nix { };

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

  # Deduped view of nanopynixVersions used for test exposure/CI, so aliased
  # names (e.g. `stable`/`latest`) don't produce redundant test jobs. Note
  # `nanopynixVersions.stable` below is still the full, non-deduped set.
  nanopynixTestVersions = dedupeVersions nanopynixVersions;

  nanopynixVersionNames = builtins.attrNames nanopynixTestVersions;

  # Per-version test runners, exposed individually as `nanopynix-tests-<name>`
  # flake packages so CI can build/run each Nix version in its own job.
  nanopynixVersionTests = lib.mapAttrs' (
    name: value: lib.nameValuePair "nanopynix-tests-${name}" value.tests
  ) nanopynixTestVersions;

  nanopynix-all-tests = pkgs.callPackage ./nix/nix-version-tests.nix {
    nanopynixVersions = nanopynixTestVersions;
    inherit (inputs) nixpkgs;
  };

in
{
  inherit (pkgs) lib;

  inherit (nanopynixVersions.stable)
    nanopynix
    nanopynix-bindings
    pynix
    shell
    tests
    nanopynix-docs
    ;

  inherit
    flake
    pkgs
    nanopynixVersions
    nanopynixVersionNames
    nanopynix-all-tests
    nanopynix-proto
    clypi
    grpclib-transports
    pyproject-nix
    ;
}
// nanopynixVersionTests
