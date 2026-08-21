{
  inputs = {
    flake-compatish.url = "github:lillecarl/flake-compatish";
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    # A test fixture, and nothing else. `pynix`'s LSP ships an
    # `EasykubenixDialect` (pynix-lsp/src/pynix_lsp/_easykubenix.py), and the
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
      # **`meta.platforms` decides what each system exports, and no list here
      # does.** `nanopynix-store-exec` rearranges the mount table and links
      # `glibc.static`, so `tools/store-exec/package.nix` declares
      # `lib.platforms.linux`, which is right. Exporting it on macOS anyway
      # left an attribute that names a package and throws when anything forces
      # it, so `nix flake show` and `nix flake check` failed there on a tool
      # that could never have run.
      #
      # `availableOn` reads that declaration, so a package states its own
      # platforms once and this filter answers for every system. Add nothing
      # here for the next Linux-only tool; give the package the `meta` it
      # deserves. Issue #148.
      #
      # Reading `meta` of a package that this platform refuses is safe: the
      # refusal replaces `outPath` and `drvPath`, and leaves `meta` alone.
      packages = forAllSystems (
        system:
        lib.filterAttrs (
          _: value:
          lib.isDerivation value && lib.meta.availableOn eachDefNix.${system}.pkgs.stdenv.hostPlatform value
        ) eachDefNix.${system}
      );
      # The standard place for the four static gates, although `nix flake
      # check` cannot run them today: that command evaluates every package,
      # and `packages.shell` fails a pure evaluation with "Editable root was
      # passed as a Nix store path string". Build the `check-*` packages
      # above instead, which is what CI does.
      checks = forAllSystems (system: eachDefNix.${system}.checks);
      devShells = forAllSystems (system: {
        default = eachDefNix.${system}.shell;
        editable = eachDefNix.${system}.shell;
        nonEditable = eachDefNix.${system}.nonEditableShell;
        non-editable = eachDefNix.${system}.nonEditableShell;
      });
      # The service modules this repository ships, and the answer issue #131
      # asked for. Not per-system: a module is evaluated by the configuration
      # that imports it, which knows its own system.
      #
      # Each wrapper is what sets `services.pynixd.package`. The module files
      # themselves state no default: the option used to read
      # `pynixd/default.nix` and that project's own `flake.lock`, which built a
      # second pynixd, pinned apart from the one this repository tests.
      #
      # Both platforms take their option block from `pynixd/nix/common.nix`, so
      # the two cannot drift.
      #
      # `checks.nixos-module` evaluates the NixOS module, so a rename or a type
      # error fails a gate rather than a rebuild on someone's machine. The
      # darwin module has no such gate; `pynixd/nix/darwin/default.nix` states
      # why in its header.
      nixosModules.pynixd =
        { pkgs, lib, ... }:
        {
          imports = [ ./pynixd/nix/nixos ];
          services.pynixd.package = lib.mkDefault eachDefNix.${pkgs.stdenv.hostPlatform.system}.pynixd;
        };
      nixosModules.default = inputs.self.nixosModules.pynixd;
      darwinModules.pynixd =
        { pkgs, lib, ... }:
        {
          imports = [ ./pynixd/nix/darwin ];
          services.pynixd.package = lib.mkDefault eachDefNix.${pkgs.stdenv.hostPlatform.system}.pynixd;
        };
      darwinModules.default = inputs.self.darwinModules.pynixd;
      legacyPackages = forAllSystems (system: inputs.nixpkgs.legacyPackages.${system});
      lib = inputs.nixpkgs.lib;
    };
}
