# Run the environment that the graph built, inside the store that holds it.
#
# **A store path is absolute, and so is every symlink that `make-venv.py`
# writes.** `run.sh` builds in a private chroot store, which puts the store
# under a prefix, so those symlinks resolve only from inside a build of that
# store. This derivation is that inside.
#
# It also depends on the output of a dynamic derivation, which is the ordinary
# consumer side of this whole feature: `builtins.outputOf` names the
# environment, and interpolating it here makes this build wait for the graph.
{
  pkgs ? import <nixpkgs> { },
  nanopynixEnv,
}:

let
  planner = import ./. { inherit pkgs nanopynixEnv; };
  venv = builtins.outputOf planner.outPath "out";
in
derivation {
  name = "venv-check";
  system = pkgs.stdenv.hostPlatform.system;
  builder = "${pkgs.bash}/bin/bash";

  args = [
    "-c"
    ''
      set -eu
      {
        "${venv}/bin/python" ${./scripts/check.py}
        echo "console script"
        printf '  idna %s\n' "$("${venv}/bin/idna" ドメイン.テスト)"
      } > "$out"
    ''
  ];
}
