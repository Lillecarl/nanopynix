{
  inputs = {
    flake-compatish.url = "github:lillecarl/flake-compatish";
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    # A test fixture, and nothing else. `pynix`'s LSP ships an
    # `EasykubenixDialect` (pynix/src/pynix/_lsp/_easykubenix.py), and the
    # scenarios in pynix/tests/test_lsp/easykubenix/ drive it against a real
    # easykubenix module tree rather than a hand-rolled `lib.evalModules`
    # stand-in -- which is the only way to test a dialect that exists to
    # understand that repository's actual option structure.
    #
    # Nothing this repository *builds* comes from here. The `ekn` CLI used to
    # live in this tree and was moved to easykubenix, which owns it outright:
    # ekn reads a fixed Nix-to-JSON schema that easykubenix's own modules
    # produce, so the two have to change together, and pynix no longer has an
    # `ekn` subcommand.
    easykubenix = {
      url = "github:Lillecarl/easykubenix";
      inputs.nixpkgs.follows = "nixpkgs";
      # easykubenix depends on a *published* nanopynix, and this input exists
      # to supply Nix modules, not a Python closure. Cutting the return edge
      # keeps that copy's own inputs -- grpclib-transports among them -- out
      # of this lockfile entirely, which matters because grpclib-transports is
      # vendored here now (grpclib-transports/) and a second, published copy
      # of it in the closure would be a confusing thing to reason about.
      inputs.nanopynix.follows = "";
      # One flake-compatish in this lockfile, and not two. Without this,
      # easykubenix pins its own copy, Nix names one node `flake-compatish`
      # and the other `flake-compatish_2`, and which name lands on which is
      # not ours to choose. `nix/compat.nix` follows the root input mapping
      # rather than the node name, so it reads the right one either way -- but
      # a second copy is still a second revision of the code that every
      # `--file .` evaluation goes through.
      inputs.flake-compatish.follows = "flake-compatish";
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
      # The finished products, and nothing else.
      #
      # This used to flatten three more sets into one namespace: every
      # per-version test runner as `nanopynix-tests-<version>`, every
      # per-version library as `nanopynix-<version>`, and every gate as
      # `check-<name>`. All three existed for one reason -- CI selected what
      # to build with `nix build ".#<name>"`, and a flake output set is flat,
      # so a nested attribute had to be given a mangled flat name to be
      # reachable.
      #
      # `nix build --file . <attrpath>` reaches any attribute, and
      # `FLAKE_COMPATISH_DISABLE_OVERRIDES=1` makes that evaluation agree with
      # a flake evaluation, so CI names `ciSteps.nix_2_34-tsan` and
      # `checks.lint` as they are written. The mangling had no other consumer.
      packages = forAllSystems (system: lib.filterAttrs (_: lib.isDerivation) eachDefNix.${system});
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
