let
  flake-compatish = import (
    fetchTree (builtins.fromJSON (builtins.readFile ../flake.lock)).nodes.flake-compatish.locked
  );
in
flake-compatish {
  source = ../.;
  overrides = {
    self = ../.;
    # nixpkgs = <nixpkgs>;
    grpclib-transports = /home/lillecarl/Code/grpclab;
  };
  nixpkgsArgs = system: {
    inherit system;
    config.allowUnfree = true;
  };
}
