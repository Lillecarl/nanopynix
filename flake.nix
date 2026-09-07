# The public surface of this repository, for a consumer who uses flakes.
#
# **This is not how the repository builds.** `default.nix` is, and the nixidae
# umbrella hands it every source. This file exists because flakes have the
# market share: it lets somebody write an input for this repository and get a
# curated set of outputs, rather than nothing.
#
# So it holds no logic. It names what is public and calls `default.nix`, and
# a change to how anything is built happens there.
#
# Two inputs, and neither duplicates the umbrella's pins.
#
#   nixpkgs   The consumer's, and the point of the exercise. It is handed to
#             the umbrella in place of the revision nix/sources.lock names,
#             so `inputs.<this>.inputs.nixpkgs.follows = "nixpkgs"` does what
#             a flake user expects it to. Measured on pynixd: with the
#             umbrella's own revision the flake and `--file .` give the same
#             derivation, d3w8gnxlihcrdvz56fwqlwm4k4r3p6ba.
#
#   nixidae   Which umbrella, and nothing else. `nix/sources.nix` finds one
#             by an impure fetch, which a flake evaluation cannot do, so the
#             lock beside this file pins it instead. Every other source comes
#             from that revision's own nix/sources.lock.
#
# `flake.lock` here therefore has two nodes and pins nothing twice.
{
  description = "Drive Nix from Python, batteries included";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";
    nixidae = {
      url = "github:nixidae/nixidae";
      flake = false;
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      nixidae,
    }:
    let
      inherit (nixpkgs) lib;
      forAllSystems = lib.genAttrs lib.systems.flakeExposed;

      # The umbrella's own set, with the consumer's nixpkgs in place of the
      # one it names.
      sources = import "${nixidae}/nix/wire.nix" {
        overrides.nixpkgs = nixpkgs.outPath;
      };

      each = forAllSystems (
        system:
        import ./. {
          inherit sources system;
          pkgs = import nixpkgs {
            inherit system;
            config.allowUnfree = true;
          };
        }
      );
    in
    {
      # The finished products, and no development environment among them.
      #
      # **A shell cannot be a package here.** `shell`, `nonEditableShell`,
      # `pynixDevEnv` and `pynixNonEditableDevEnv` install this repository as
      # an editable root, and a flake evaluation copies the source to the
      # store first, so each one fails with
      #
      #   Pass editable root either as a string pointing to an absolute path
      #   non-store path, or use environment variables for relative paths.
      #
      # A single attribute that throws takes `nix flake show` and `nix flake
      # check` down with it, so this names what it exports rather than
      # filtering a set that holds them.
      #
      # `meta.platforms` decides the rest: a Linux-only tool is absent
      # elsewhere rather than an attribute that throws when forced.
      packages = forAllSystems (
        system:
        lib.filterAttrs (_: value: lib.meta.availableOn each.${system}.pkgs.stdenv.hostPlatform value) {
          inherit (each.${system})
            nanopynix
            nanopynix-bindings
            nanopynix-helpers
            nanopynix-proto
            nanopynix-docs
            nanopynixWheel
            nanopynixWheelLicenses
            libpynix
            pynix
            pynix-lsp
            pytest-agent
            storeExecTool
            tofuCoreSchemaTool
            ;
          default = each.${system}.nanopynix;
        }
      );

      # The gates, filtered twice.
      #
      # `checks` comes from `callPackage`, so `makeOverridable` adds
      # `override` and `overrideDerivation` to it, and neither is a
      # derivation `nix flake check` can realise.
      #
      # `shell` is dropped for the reason `packages` gives: it installs this
      # repository as an editable root, and a flake evaluation has copied the
      # source to the store by then. `nix build --file . checks.shell` runs
      # it, and that is what CI does.
      checks = forAllSystems (
        system:
        lib.filterAttrs (name: value: lib.isDerivation value && name != "shell") each.${system}.checks
      );

      # `nix develop --impure`, and not `nix develop`. This installs the
      # repository as an editable root, which a pure evaluation cannot do --
      # see the note on `packages` above.
      devShells = forAllSystems (system: {
        default = each.${system}.shell;
      });
    };
}
