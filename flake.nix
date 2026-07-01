{
  inputs = {
    flake-compatish.url = "github:lillecarl/flake-compatish";
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";
  };
  outputs =
    inputs:
    let
      eachSys = (
        inputs.nixpkgs.lib.genAttrs (
          # system tuplets built by NixOS Hydra
          builtins.fromJSON (builtins.readFile "${inputs.nixpkgs}/ci/supportedSystems.json")
        )
      );
      eachPkgs = eachSys (system: import inputs.nixpkgs { inherit system; });
      eachDefNix = eachSys (system: import ./. { pkgs = eachPkgs.${system}; });
    in
    {
      packages = eachSys (
        system:
        let
          defNix = eachDefNix.${system};
        in
        {
          default = defNix.package;
          pynixd = defNix.package;
          libpynixd = defNix.library;
        }
      );
      devShells = eachSys (
        system:
        let
          defNix = eachDefNix.${system};
        in
        {
          default = defNix.shell;
        }
      );
      legacyPackages = eachSys (system: eachPkgs.${system});
      nixosModules.default = ./nix/nixos;
    };
}
