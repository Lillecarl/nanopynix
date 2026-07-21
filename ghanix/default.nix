# ghanix -- render GitHub Actions workflows from a NixOS-module-system
# schema, evaluated down to plain YAML-able values (e.g. via
# `builtins.toYAML`). Grown inside nanopynix, which is its first real
# consumer, but nothing under this directory knows that: it only needs
# `lib` (nixpkgs' `lib`, for `lib.evalModules`/`lib.types`/`lib.asserts`),
# never a flake, a store path, or any other project-specific plumbing. That
# split is deliberate -- it's what would let this directory be lifted into
# its own project unchanged.
#
# Usage:
#   ghalib = import ./ghanix { inherit lib; };
#   ghalib.evalWorkflow {
#     name = "On commit";
#     on = { push = { }; };
#     jobs.build.steps = [ (ghalib.steps.checkout { }) ];
#   }
{ lib }:
let
  schema = import ./schema.nix { inherit lib; };
  steps = import ./steps.nix { inherit lib; };
in
schema // steps
