{
  inputs = {
    flake-compatish.url = "github:lillecarl/flake-compatish";
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";
    grpclib-transports = {
      url = "github:lillecarl/grpclib-transports";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    easykubenix = {
      url = "github:Lillecarl/easykubenix";
      inputs.nixpkgs.follows = "nixpkgs";
      # easykubenix depends on a *published* nanopynix, which drags in its own
      # grpclib-transports. Without this, the lockfile carries two
      # grpclib-transports nodes and ours is the one renamed to
      # `grpclib-transports_2` -- which silently disables the local-checkout
      # override in nix/compat.nix, because flake-compatish keys overrides by
      # lockfile node name rather than by input name. Pointing easykubenix at
      # this tree is also just correct: we *are* nanopynix, and testing ekn
      # against a months-old published copy of it is not what anyone wants.
      inputs.nanopynix.follows = "";
    };
    # numtide's fork, not nix-community/tree-sitter-nix: numtide's README
    # states it's "kept moving while upstream is stalled" and "new bug
    # reports and PRs should be filed here" -- nixpkgs itself still pins the
    # stalled nix-community rev. Not a flake (grammar source only), built via
    # nix/tree-sitter-nix.nix.
    tree-sitter-nix-numtide = {
      url = "github:numtide/tree-sitter-nix";
      flake = false;
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
        lib.filterAttrs (_: v: lib.isDerivation v) (
          eachDefNix.${system}
          // eachDefNix.${system}.tests
          // lib.mapAttrs' (
            n: v: lib.nameValuePair "nanopynix-${n}" v.nanopynix
          ) eachDefNix.${system}.nanopynixVersions
        )
      );
      devShells = forAllSystems (system: {
        default = eachDefNix.${system}.shell;
      });
      legacyPackages = forAllSystems (system: inputs.nixpkgs.legacyPackages.${system});
      lib = inputs.nixpkgs.lib;
    };
}
