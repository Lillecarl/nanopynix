# Every example, in one attribute set.
#
#   nix build --file ./ddrn/examples hello.result --no-link --print-out-paths -L
#   nix build --file ./ddrn/examples select.result --no-link --print-out-paths -L
#   nix build --file ./ddrn/examples venv.result --no-link --print-out-paths -L
#
# Each one needs `--extra-experimental-features 'ca-derivations
# dynamic-derivations'`, which `ddrn/examples/shell.nix` sets for a shell.
{
  pkgs ? import <nixpkgs> { },
}:

let
  ddrn = pkgs.callPackage ../nix/planner.nix { };
in
{
  inherit (ddrn) mkPlanner candidate;

  hello = pkgs.callPackage ./hello { inherit ddrn; };
  select = pkgs.callPackage ./select { inherit ddrn; };
  venv = pkgs.callPackage ./venv { inherit ddrn; };
}
