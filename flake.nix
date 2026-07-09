{
  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";
  };
  outputs =
    inputs:
    let
      lib = inputs.nixpkgs.lib;
      forAllSystems = lib.genAttrs lib.systems.flakeExposed;
      eachPkgs = forAllSystems (system: import inputs.nixpkgs { inherit system; });
      eachDefNix = forAllSystems (system: import ./. { pkgs = eachPkgs.${system}; });
    in
    {
      packages = forAllSystems (system: {
        inherit (eachDefNix.${system}) nanopynix-bindings nanopynix;
      });
      legacyPackages = forAllSystems (system: eachPkgs.${system});
    };
}
