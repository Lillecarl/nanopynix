let
  # flake-compatish = import (
  #   fetchTree (builtins.fromJSON (builtins.readFile ../flake.lock)).nodes.flake-compatish.locked
  # );
  flake-compatish = import ../../flake-compatish;
in
flake-compatish {
  source = ../.;
  overrides = {
    self = ../.;
    nixpkgs = <nixpkgs>;
    grpclib-transports = ../../grpclab;
  };
  nixpkgsArgs = system: {
    inherit system;
    config.allowUnfree = true;
  };
}
