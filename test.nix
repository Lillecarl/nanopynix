{
  pkgs ? import <nixpkgs> { },
}:

let
  ts =
    let
      v = builtins.getEnv "PYNIXD_TEST_TS";
    in
    if v == "" then toString builtins.currentTime else v;

  # Simple derivation helper that sleeps to simulate build pressure.
  # Uses currentTime in the output to ensure every derivation is unique per eval.
  mkDrv =
    {
      name,
      deps ? [ ],
      sleepSecs ? 1,
      text ? "",
    }:
    pkgs.stdenvNoCC.mkDerivation {
      name = "${name}-${ts}";
      buildInputs = deps;
      dontUnpack = true;
      _timestamp = ts;
      buildPhase = ''
        echo "Building ${name} (sleeping ${toString sleepSecs}s)..."
        sleep ${toString sleepSecs}
        mkdir -p $out/bin
        cat > $out/bin/${name} << SCRIPT
        #!/bin/sh
        echo "${name}: ${text} (built at ${ts})"
        SCRIPT
        chmod +x $out/bin/${name}
      '';
      installPhase = "true";
    };

  # Generate N independent leaves with a root depending on all of them.
  # parId: unique prefix per client to avoid deduplication
  # leaves: number of leaf derivations
  # sleepSecs: how long each leaf sleeps (0 for benchmark, 2 for pressure test)
  mkParallel =
    {
      leaves,
      sleepSecs,
      parId ? "",
    }:
    let
      prefix = if parId == "" then "" else "${parId}-";
      parLeaves = builtins.genList (
        i:
        mkDrv {
          name = "${prefix}par-${toString i}";
          inherit sleepSecs;
          text = "parallel ${prefix}${toString i} ${ts}";
        }
      ) leaves;
    in
    mkDrv {
      name = "${prefix}par-root";
      deps = parLeaves;
      sleepSecs = 0;
      text = "parallel root ${ts}";
    };

  # ── Layer 0: 4 leaf nodes (no deps on other test drvs) ──────────
  a0 = mkDrv {
    name = "leaf-a0";
    sleepSecs = 2;
    text = "leaf a0";
  };
  a1 = mkDrv {
    name = "leaf-a1";
    sleepSecs = 1;
    text = "leaf a1";
  };
  a2 = mkDrv {
    name = "leaf-a2";
    sleepSecs = 3;
    text = "leaf a2";
  };
  a3 = mkDrv {
    name = "leaf-a3";
    sleepSecs = 1;
    text = "leaf a3";
  };

  # ── Layer 1: 6 mid nodes, unbalanced deps ───────────────────────
  # a0 depended on by: b0, b1              (2 dependents)
  # a1 depended on by: b1, b2              (2 dependents)
  # a2 depended on by: b2, b3, b5          (3 dependents — hot path)
  # a3 depended on by: b4, b5              (2 dependents)
  b0 = mkDrv {
    name = "mid-b0";
    deps = [ a0 ];
    sleepSecs = 1;
    text = "depends a0";
  };
  b1 = mkDrv {
    name = "mid-b1";
    deps = [
      a0
      a1
    ];
    sleepSecs = 1;
    text = "depends a0+a1";
  };
  b2 = mkDrv {
    name = "mid-b2";
    deps = [
      a1
      a2
    ];
    sleepSecs = 2;
    text = "depends a1+a2";
  };
  b3 = mkDrv {
    name = "mid-b3";
    deps = [ a2 ];
    sleepSecs = 1;
    text = "depends a2";
  };
  b4 = mkDrv {
    name = "mid-b4";
    deps = [ a3 ];
    sleepSecs = 1;
    text = "depends a3";
  };
  b5 = mkDrv {
    name = "mid-b5";
    deps = [
      a2
      a3
    ];
    sleepSecs = 1;
    text = "depends a2+a3";
  };

  # ── Layer 2: 3 top nodes (fan-in) ──────────────────────────────
  c0 = mkDrv {
    name = "top-c0";
    deps = [
      b0
      b1
      b2
    ];
    sleepSecs = 1;
    text = "fan-in b0+b1+b2";
  };
  c1 = mkDrv {
    name = "top-c1";
    deps = [
      b3
      b4
    ];
    sleepSecs = 1;
    text = "fan-in b3+b4";
  };
  c2 = mkDrv {
    name = "top-c2";
    deps = [
      b4
      b5
    ];
    sleepSecs = 1;
    text = "fan-in b4+b5";
  };

  # ── Root: depends on all tops ───────────────────────────────────
  root = mkDrv {
    name = "root";
    deps = [
      c0
      c1
      c2
    ];
    sleepSecs = 1;
    text = "root";
  };

  # ── Env-configured parallel (for test_parallel.py compat) ───────
  parCount =
    let
      v = builtins.getEnv "PYNIXD_PAR_COUNT";
    in
    if v == "" then 100 else builtins.fromJSON v;
  parId =
    let
      v = builtins.getEnv "PYNIXD_PAR_ID";
    in
    if v == "" then "" else v;
  parSleep =
    let
      v = builtins.getEnv "PYNIXD_PAR_SLEEP";
    in
    if v == "" then 2 else builtins.fromJSON v;

in
{
  # Single simple derivation (original test)
  simple = pkgs.writeShellApplication {
    name = "test-${ts}";
    text = "hello ${ts}";
  };

  # Multi-layer unbalanced DAG (14 derivations: 4 + 6 + 3 + 1)
  dag = root;

  # Alias for compatibility
  complex = root;

  # Root depending on N independent leaves
  # PYNIXD_PAR_COUNT (default 100), PYNIXD_PAR_SLEEP (default 2),
  # PYNIXD_PAR_ID (default "")
  parallel = mkParallel {
    leaves = parCount;
    sleepSecs = parSleep;
    inherit parId;
  };

  # Zero-sleep variant for benchmarking pure pynixd overhead
  bench = mkParallel {
    leaves = parCount;
    sleepSecs = 0;
    inherit parId;
  };

  # Large output for NAR streaming tests
  bench-100mb = mkParallel {
    leaves = 100;
    sleepSecs = 0;
    parId = "bench-100mb";
  };

  # Truly big output using dd
  big = pkgs.stdenvNoCC.mkDerivation {
    name = "big-${ts}";
    dontUnpack = true;
    buildPhase = ''
      mkdir -p $out
      dd if=/dev/zero of=$out/big-file bs=1M count=10
    '';
    installPhase = "true";
  };
}
