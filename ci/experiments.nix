# The CI experiments, and the one interface that `on_experiment.nix` runs.
#
# **An experiment is a package here, and not a script in a YAML file.** A
# workflow becomes available to `workflow_dispatch` only after that workflow
# reaches the default branch, so a new YAML file costs a push to `develop`
# before it can run once. A new attribute in this file costs nothing: the
# generic runner already carries the name as an input, and the branch that
# holds the experiment is an input too.
#
# Each experiment reads its matrix cell from `MATRIX_JSON`, which the runner
# sets to `toJSON(matrix)`. An experiment that takes no matrix ignores it.
#
# Run one the way CI runs it:
#
#   MATRIX_JSON='{"arm":"amplified"}' nix run --file . experiments.gc-soak
{
  pkgs,
  tests,
}:
let
  inherit (pkgs) lib;

  # The arguments that every full-suite experiment gives the packaged runner.
  # The runner has no pytest-agent, so `-rsxXfE` and a short traceback are the
  # only detail that a failure leaves behind.
  suiteArgs = [
    "--verbose"
    "--tb=short"
    "-rsxXfE"
    "--run-temp-store-builds"
    "--nix-test-backends"
    "local"
    "-m"
    "not soak"
  ];

  suiteEnv = [
    "NANOPYNIX_CORE_DEBUG=1"
    "NANOPYNIX_RPC_TIMEOUT=30"
    "PYTHONDONTWRITEBYTECODE=1"
  ];

  runner = tests."nanopynix-tests-nix_2_34";
in
{
  # A collector defect of this kind reproduces only in the whole suite, and
  # only some of the time. That rate needs repetition, and the failure count of
  # each arm is the measurement.
  #
  # - control   -- the shipped configuration, and the rate to compare against.
  # - amplified -- `GC_FREE_SPACE_DIVISOR=64`, so the collector runs far more
  #                often. The rate of #70 followed the collection rate: 0 in 20
  #                with `GC_DONT_GC=1`, 1 in 30 by default, and 5 in 15 with
  #                this divisor, measured on the soak driver.
  #
  # The `interior` arm went with #70. It set
  # `NANOPYNIX_GC_ALL_INTERIOR_POINTERS=1`, which no longer exists, and its own
  # criterion was met: the arm excluded the whole displacement class, and the
  # cause turned out to be an environment that nothing rooted.
  #
  # `docs/collector-and-threads.md` records what each arm settles.
  gc-soak = pkgs.writeShellApplication {
    name = "experiment-gc-soak";
    runtimeInputs = [ pkgs.jq ];
    text = ''
      arm=$(jq -r '.arm // "control"' <<<"''${MATRIX_JSON:-{\}}")
      echo "arm: $arm"
      case "$arm" in
        control)   armEnv=(NANOPYNIX_GC_SOAK_ARM=control) ;;
        amplified) armEnv=(GC_FREE_SPACE_DIVISOR=64) ;;
        *) echo "unknown arm: $arm" >&2; exit 2 ;;
      esac
      exec env ${lib.escapeShellArgs suiteEnv} "''${armEnv[@]}" \
        ${lib.getExe' runner "nanopynix-tests"} ${lib.escapeShellArgs suiteArgs}
    '';
  };
}
