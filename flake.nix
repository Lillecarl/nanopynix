{
  inputs = {
    flake-compatish.url = "github:lillecarl/flake-compatish";
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";
    grpclib-transports = {
      url = "github:lillecarl/grpclib-transports";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };
  outputs =
    inputs:
    let
      lib = inputs.nixpkgs.lib;
      forAllSystems = lib.genAttrs lib.systems.flakeExposed;
      eachDefNix = forAllSystems (
        system:
        import ./. {
          inherit inputs system;
          pkgs = inputs.self.legacyPackages.${system};
        }
      );
    in
    {
      packages = forAllSystems (
        system:
        {
          inherit (eachDefNix.${system})
            nanopynix-bindings
            nanopynix
            nanopynix-nixVersions-tests
            pynix
            ;
        }
        // lib.mapAttrs' (
          nixVersion: nanopynix: lib.nameValuePair "nanopynix-${nixVersion}" nanopynix
        ) eachDefNix.${system}.nanopynix-nixVersions
      );
      checks = forAllSystems (system: {
        pynix = eachDefNix.${system}.pynix;
      });
      legacyPackages = forAllSystems (system: inputs.nixpkgs.legacyPackages.${system});
    };
}
