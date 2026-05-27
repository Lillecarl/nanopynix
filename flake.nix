{
  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";
    flake-compatish = {
      url = "github:lillecarl/flake-compatish";
      flake = false;
    };
  };
  outputs =
    inputs:
    let
      mapper = (
        inputs.nixpkgs.lib.genAttrs [
          "x86_64-linux"
          "aarch64-linux"
          "aarch64-darwin"
        ]
      );
    in
    {
      packages = mapper (
        system:
        let
          pkgs = import inputs.nixpkgs { inherit system; };
        in
        {
          default = (import ./. { inherit pkgs; }).package;
        }
      );
      nixosModules.default = ./nix/nixos;
    };
}
