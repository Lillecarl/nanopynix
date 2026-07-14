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
      packages = forAllSystems (system: lib.filterAttrs (_: v: lib.isDerivation v) eachDefNix.${system});
      devShells = forAllSystems (system: {
        default = eachDefNix.${system}.shell;
      });
      legacyPackages = forAllSystems (system: inputs.nixpkgs.legacyPackages.${system});
    };
}
