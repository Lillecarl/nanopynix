# `builder-rpc-v0`: the builder registers its own output.
#
# This example does NOT run under the Nix of this repository's pin. It needs a
# Nix from master, and it needs a store that the master Nix builds in itself.
# `ddrn/examples/submitted/run.sh` sets both up. Read the "Running this"
# section of `ddrn/README.md` first.
#
# The feature gives the builder a restricted daemon socket at `$NIX_REMOTE`.
# The builder creates as many store objects as it likes, and names one of them
# as its output. That is what lifts the one-derivation limit of a plain
# dynamic derivation: a planner can register a whole graph of `.drv` files and
# submit the root.
{
  pkgs ? import <nixpkgs> { },
  # The store path of a Nix that has the feature. `run.sh` passes it.
  nixMaster,
}:

derivation {
  name = "submitted-hello";
  system = pkgs.stdenv.hostPlatform.system;
  builder = "${pkgs.bash}/bin/bash";

  # The feature is requested as a *system feature*, and the store has to
  # advertise it. `run.sh` passes `--system-features builder-rpc-v0`.
  requiredSystemFeatures = [ "builder-rpc-v0" ];

  # Nix refuses the feature on a derivation that is not content-addressing:
  # "The builder-rpc-v0 feature may only be used with content-addressing
  # derivations" (`derivation-builder.cc`).
  __contentAddressed = true;
  outputHashMode = "recursive";
  outputHashAlgo = "sha256";

  PATH = "${pkgs.coreutils}/bin:${builtins.storePath nixMaster}/bin";

  args = [
    "-c"
    ''
      set -eux
      export NIX_CONFIG='extra-experimental-features = nix-command ca-derivations dynamic-derivations'

      # A `builder-rpc-v0` derivation gets no $out. The output arrives through
      # `submit-output` instead, so there is no path to write to.
      if [ -n "''${out+set}" ]; then
        echo "unexpected: out is set to '$out'" >&2
        exit 1
      fi

      # Set by Nix to the restricted socket, inside the sandbox.
      echo "NIX_REMOTE=$NIX_REMOTE"

      mkdir -p work
      echo "hello from a submitted output" > work/greeting

      # The name matters. Nix checks the name of the submitted store object
      # against the name the output must have, which is the derivation name
      # for `out`. A mismatch fails after the build with:
      #   output 'out' (at '...-work') was named 'work',
      #   expected 'submitted-hello'
      path=$(nix store add --name submitted-hello ./work)
      nix store submit-output "$path" out
    ''
  ];
}
