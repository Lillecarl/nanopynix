{
  inputs = {
    flake-compatish.url = "github:lillecarl/flake-compatish";
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    easykubenix = {
      url = "github:Lillecarl/easykubenix";
      inputs.nixpkgs.follows = "nixpkgs";
      # easykubenix depends on a *published* nanopynix. Cutting that off is
      # simply correct: we *are* nanopynix, and testing ekn against a
      # months-old published copy of it is not what anyone wants. It also
      # keeps that copy's own inputs -- grpclib-transports among them -- out
      # of this lockfile entirely, which matters because grpclib-transports
      # is vendored here now (grpclib-transports/) and a second, published
      # copy of it in the closure would be a confusing thing to reason about.
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
          # Also under `packages`, and not only under `checks` below, because
          # CI builds a job's outputs by `nix build ".#<name>"`. See
          # nix/checks.nix.
          // lib.mapAttrs' (n: v: lib.nameValuePair "check-${n}" v) eachDefNix.${system}.checks
        )
      );
      # The standard place for the four static gates, although `nix flake
      # check` cannot run them today: that command evaluates every package,
      # and `packages.shell` fails a pure evaluation with "Editable root was
      # passed as a Nix store path string". Build the `check-*` packages
      # above instead, which is what CI does.
      checks = forAllSystems (system: eachDefNix.${system}.checks);
      devShells = forAllSystems (system: {
        default = eachDefNix.${system}.shell;
      });
      legacyPackages = forAllSystems (system: inputs.nixpkgs.legacyPackages.${system});
      lib = inputs.nixpkgs.lib;
    };
}
