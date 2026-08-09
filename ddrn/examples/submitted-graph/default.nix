# `builder-rpc-v0`, driven by nanopynix: the builder registers a whole graph.
#
# `ddrn/examples/submitted` is the same feature driven by the `nix` CLI, and it
# submits one store object that the builder wrote with `nix store add`. This
# example replaces the CLI with nanopynix, and submits the root of a graph of
# three derivations that the builder built as values and rendered with Nix's
# own ATerm writer.
#
# **There is no `nix` binary in this sandbox.** nanopynix links libnixstore
# directly. Read `ddrn/examples/submitted-graph/plan.py`, and the section
# "The socket is an allowlist of six operations" in `ddrn/README.md`.
#
# This example does NOT run under the Nix of this repository's pin. It needs a
# Nix from master, and a nanopynix built against that same Nix.
# `ddrn/examples/submitted-graph/run.sh` sets both up, and then realises the
# submitted `.drv` to prove that the graph is real.
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
  # **The `.drv` suffix is required, and `plan.py` must agree with it.** The
  # submitted store object is the root `.drv` of the graph, whose name is
  # `<root name>.drv`. Nix checks that name against the name the output must
  # carry, which for `out` is the name of this derivation
  # (`derivation-check.cc:109`).
  name = "graph.drv";
  system = pkgs.stdenv.hostPlatform.system;
  builder = "${pkgs.bash}/bin/bash";

  # The feature is requested as a *system feature*, and the store has to
  # advertise it. `run.sh` passes `--system-features builder-rpc-v0`.
  requiredSystemFeatures = [ "builder-rpc-v0" ];

  # Nix refuses the feature on a derivation that is not content-addressing:
  # "The builder-rpc-v0 feature may only be used with content-addressing
  # derivations" (`derivation-builder.cc:482`, which tests `type(drv).isCA()`).
  #
  # **The mode is `text`, and it has to be.** A derivation whose name ends in
  # `.drv` is accepted only when it ingests as text and has exactly one output
  # named `out` (`primops.cc:1815`). Text ingestion is content-addressing, so
  # this satisfies both rules at once, and it is the same mode that an ordinary
  # dynamic derivation uses -- see `ddrn/nix/planner.nix`. That is the honest
  # description of this derivation: it *is* a dynamic derivation, and what
  # `builder-rpc-v0` changed is only how its builder produced the `.drv`.
  __contentAddressed = true;
  outputHashMode = "text";
  outputHashAlgo = "sha256";

  # What the plan needs, and nothing else. Each one reaches the sandbox
  # because naming it here makes it an input, and each is an `input_srcs`
  # entry of every derivation that the plan registers.
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
