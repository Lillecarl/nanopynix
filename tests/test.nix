{
  pkgs ? import <nixpkgs> { },
}:

let
  system = builtins.currentSystem;

  ts =
    let
      v = builtins.getEnv "PYNIXD_TEST_TS";
    in
    if v == "" then toString builtins.currentTime else v;

  modes = {
    standard = import ./modes/standard.nix { inherit pkgs system ts; };
    ca = import ./modes/ca.nix { inherit system ts; };
    dyn = import ./modes/dyn-drv.nix { inherit system ts; };
    minimal = import ./modes/minimal.nix { inherit system ts; };
  };

in
modes.standard
// {
  ca = modes.ca;
  dyn = modes.dyn;
  minimal = modes.minimal;
}
