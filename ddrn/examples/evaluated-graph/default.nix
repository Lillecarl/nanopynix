# `builder-rpc-v0` with the evaluator, and with a planner that names itself.
#
# `ddrn/examples/submitted-graph` is the same graph, built for the protocol as
# it was released. Two changes to Nix separate this example from that one, and
# `ddrn/UPSTREAM.md` gives both:
#
# 1. The allowlist of the restricted socket permits `EnsurePath`, so
#    `builtins.storePath` works and the evaluator runs. `plan.py` therefore
#    writes a Nix expression, and no ATerm.
# 2. A submitted output that is a derivation may carry any name, because Nix
#    verifies the derivation instead of comparing two names.
#
# **The second change is what this file shows.** The derivation below is named
# `planner`, and the root that it submits is named `graph`. The released rule
# forces the two names to agree, so `ddrn/examples/submitted-graph/default.nix`
# is named `graph.drv`: it declares, in advance, the name of a result that its
# own builder computes.
#
# This example does NOT run under the Nix of this repository's pin. It needs
# the patched Nix that `nix/nix-master.nix` reads.
# `ddrn/examples/evaluated-graph/run.sh` sets it up.
{
  pkgs ? import <nixpkgs> { },
  # A Python environment that has nanopynix, built against the same Nix that
  # builds this derivation. `run.sh` passes the store path of
  # `nanopynixMaster.pythonSet.mkVirtualEnv`. The two Nix versions must agree:
  # nanopynix links libnixstore, and the restricted socket speaks the worker
  # protocol of the Nix that opened it.
  nanopynixEnv,
}:

let
  planner = ./plan.py;
in
derivation {
  # **The name says what this derivation is, and not what it makes.** Nothing
  # here has to agree with `plan.py` any more.
  name = "planner";
  system = pkgs.stdenv.hostPlatform.system;
  builder = "${pkgs.bash}/bin/bash";

  # The feature is requested as a *system feature*, and the store has to
  # advertise it. `run.sh` passes `--system-features builder-rpc-v0`.
  requiredSystemFeatures = [ "builder-rpc-v0" ];

  # Nix refuses the feature on a derivation that is not content-addressing.
  #
  # **The mode is `text`, and it has to be.** The submitted object is a
  # derivation, every derivation ingests as text, and `checkCAOutput` compares
  # the method that this derivation declares with the method of the object that
  # the builder submitted (`derivation-check.cc`, the `CAFloating` branch). The
  # name is now free; the ingestion method is not.
  __contentAddressed = true;
  outputHashMode = "text";
  outputHashAlgo = "sha256";

  # The evaluator inside the sandbox reads this.
  NIX_CONFIG = "experimental-features = nix-command ca-derivations dynamic-derivations";

  # What the plan needs, and nothing else. Each one reaches the sandbox because
  # naming it here makes it an input, and `isAllowed` accepts an input of this
  # build. That is exactly the set that `builtins.storePath` may name.
  DDRN_SYSTEM = pkgs.stdenv.hostPlatform.system;
  DDRN_BASH = pkgs.bash;
  DDRN_COREUTILS = pkgs.coreutils;

  args = [
    "-c"
    ''
      set -eu
      # Nix runs a builder with the *basename* of the builder as `argv[0]`, and
      # CPython derives `sys.prefix` from `argv[0]`. A virtual environment
      # therefore has to be entered through its full path, which is why bash is
      # the builder here and Python is not.
      exec ${builtins.storePath nanopynixEnv}/bin/python ${planner}
    ''
  ];
}
