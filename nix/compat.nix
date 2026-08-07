let
  lock = builtins.fromJSON (builtins.readFile ../flake.lock);

  # Follow the root node's input mapping, and do not name the node directly.
  #
  # **Two nodes of this lockfile are called flake-compatish.** `easykubenix`
  # depends on it as well, so Nix gives one of them the plain name and the
  # other `flake-compatish_2`, and which one gets which is not ours to choose.
  # `nodes.flake-compatish` was easykubenix's copy, so every `--file .`
  # evaluation used the revision that *that* flake pins rather than the one
  # `flake.nix` declares. The two differed by four months, and the newer one
  # carries `FLAKE_COMPATISH_DISABLE_OVERRIDES`, so the flag read as absent
  # here while the lockfile said it was present.
  flake-compatish = import (
    fetchTree lock.nodes.${lock.nodes.${lock.root}.inputs.flake-compatish}.locked
  );
in
flake-compatish {
  source = ../.;
  overrides = {
    self = ../.;
    # nixpkgs = <nixpkgs>;
  };
  nixpkgsArgs = system: {
    inherit system;
    config.allowUnfree = true;
  };
}
