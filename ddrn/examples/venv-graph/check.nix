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
  # The tree that the editable install reads. `default.nix` redirects the
  # install at `$DDRN_EDITABLE_ROOT`, which this derivation sets, so a
  # different tree here reads back differently **from the same environment**.
  # `run.sh` proves that by building this twice.
  editableTree ? ./fixtures/myapp,
}:

let
  planner = import ./. { inherit pkgs nanopynixEnv; };
  venv = builtins.outputOf planner.outPath "out";
in
derivation {
  name = "venv-check";
  system = pkgs.stdenv.hostPlatform.system;
  builder = "${pkgs.bash}/bin/bash";

  DDRN_EDITABLE_ROOT = "${editableTree}";

  args = [
    "-c"
    ''
      set -eu
      {
        "${venv}/bin/python" ${./scripts/check.py}

        # **A binary wheel needs the loader of this system.** The `ninja` wheel
        # ships an ELF executable that names `/lib64/ld-linux-x86-64.so.2`, and
        # this system has no such file. The node that installed it ran
        # `auto-patchelf`, so the console script runs.
        echo "ninja"
        printf '  %s\n' "$("${venv}/bin/ninja" --version)"

        echo "console script"
        printf '  idna %s\n' "$("${venv}/bin/idna" ドメイン.テスト)"
        printf '  myapp %s\n' "$("${venv}/bin/myapp")"
      } > "$out"
    ''
  ];
}
