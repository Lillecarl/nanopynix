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
#   MATRIX_JSON='{"arm":"interior"}' nix run --file . experiments.gc-soak
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
  # TEMPORARY, for issue #70. Delete this attribute with the issue.
  #
  # #70 reproduces only in the whole suite, at about 1 failure in 5 runs. That
  # rate needs repetition. The failure count of each arm is the measurement.
  #
  # - control   -- the shipped configuration, and the rate to compare against.
  # - interior  -- every displacement valid. `GC_push_contents_hdr` consults
  #                `GC_valid_offsets` in the mark path, and it black-lists a
  #                reference at a displacement that Boehm does not know rather
  #                than marking the object of that reference. Nix registers 1
  #                to 7, and the bit-packed `Value` uses 3 bits. A clean arm
  #                says an unregistered displacement is the cause, and a dirty
  #                arm excludes that whole class.
  # - amplified -- `GC_FREE_SPACE_DIVISOR=64`, so the collector runs far more
  #                often. The rate of #70 already follows the collection rate:
  #                0 in 20 with `GC_DONT_GC=1`, 1 in 30 by default, and 5 in
  #                15 with this divisor, measured on the soak driver.
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
        interior)  armEnv=(NANOPYNIX_GC_ALL_INTERIOR_POINTERS=1) ;;
        amplified) armEnv=(GC_FREE_SPACE_DIVISOR=64) ;;
        *) echo "unknown arm: $arm" >&2; exit 2 ;;
      esac
      exec env ${lib.escapeShellArgs suiteEnv} "''${armEnv[@]}" \
        ${lib.getExe' runner "nanopynix-tests"} ${lib.escapeShellArgs suiteArgs}
    '';
  };
}
