# A Python environment, resolved by `packaging` and built as a graph.
#
# `ddrn/examples/venv` is the same environment under the released protocol. It
# makes the same packaging decisions and then emits **one** derivation, because
# a planner that writes to `$out` can emit exactly one. Two things follow, and
# this example removes both:
#
#   1. Every wheel is installed in a single build step, so a change to one
#      wheel rebuilds all of them.
#   2. A source distribution cannot be built at all. The backend that builds it
#      would have to exist before the plan runs, and the plan is what chooses
#      the backend.
#
# **The Nix half here knows nothing about Python.** It reads the lock file,
# makes a `fetchurl` for every artefact, and hands the planner a menu. The
# planner decides which artefacts this target needs, and what the graph over
# them is. `graph.nix` says how one node is built.
#
# This example does NOT run under the Nix of this repository's pin. It needs
# the patched Nix that `nix/nix-master.nix` reads.
# `ddrn/examples/venv-graph/run.sh` sets it up.
{
  pkgs ? import <nixpkgs> { },
  # A Python environment that has nanopynix, built against the same Nix that
  # builds this derivation. `run.sh` passes the store path of
  # `nanopynixMaster.pythonSet.mkVirtualEnv`.
  nanopynixEnv,
  # One entry for each local project to install as a PEP 660 editable.
  #
  #   src   the project tree, which Nix copies into the store. The backend
  #         reads it, and the environment does not.
  #   root  what the install redirects to. A literal path names one tree
  #         forever. A reference to a variable, which is the default, keeps
  #         the environment the same bytes for every tree, so the tree can
  #         change with no rebuild.
  editables ? {
    myapp = {
      src = ./fixtures/myapp;
      root = "$DDRN_EDITABLE_ROOT";
    };
  },
}:

let
  lock = builtins.fromJSON (builtins.readFile ./lock.json);

  # One `fetchurl` per artefact. `fetchurl` is a fixed-output derivation, so
  # instantiating it costs nothing and downloads nothing. The lock file lists
  # 21 artefacts, and this build downloads the ones that the planner picks.
  fetch =
    entry:
    pkgs.fetchurl {
      inherit (entry) url;
      hash = "sha256:${entry.sha256}";
      name = entry.filename;
    };

  # **The two `unsafeDiscard*` builtins are what keep the menu lazy.**
  # `unsafeDiscardOutputDependency` makes the `.drv` an input source of this
  # derivation while the *output* stays unbuilt, so the planner can name the
  # derivation without causing the download. `unsafeDiscardStringContext`
  # gives the planner the output path with no dependency on it.
  #
  # `graph.nix` puts the dependency back, with `builtins.appendContext`, for
  # the artefacts that the planner chooses.
  row =
    entry:
    let
      drv = fetch entry;
    in
    (builtins.removeAttrs entry [ "url" ])
    // {
      drv = builtins.unsafeDiscardOutputDependency drv.drvPath;
      out = builtins.unsafeDiscardStringContext drv.outPath;
    };

  # `packaging` is what the planner needs, and only the planner needs it. The
  # environment that comes out carries no trace of it. It is pure Python, so it
  # goes on `PYTHONPATH` beside the nanopynix environment.
  packagingPath = "${pkgs.python3Packages.packaging}/${pkgs.python3.sitePackages}";

  menu = {
    inherit (lock) roots installer;
    artifacts = map row lock.artifacts;
    tools = {
      bash = "${pkgs.bash}";
      coreutils = "${pkgs.coreutils}";
      unzip = "${pkgs.unzip}";
      gnutar = "${pkgs.gnutar}";
      gzip = "${pkgs.gzip}";
      python = "${pkgs.python3}";
      # The three below reach only a node that installs a binary wheel.
      patchelf = "${pkgs.patchelf}";
      # `pkgs.autoPatchelfHook` is a setup hook, and a graph node runs no setup
      # hook. `pkgs.auto-patchelf` is the program that the hook calls.
      autoPatchelf = "${pkgs.auto-patchelf}";
      bintools = "${pkgs.stdenv.cc.bintools}";
      gccLib = "${pkgs.stdenv.cc.cc.lib}";
    };
    scripts = {
      unpackWheel = "${./scripts/unpack-wheel.sh}";
      installWheel = "${./scripts/install-wheel.sh}";
      buildSdist = "${./scripts/build-sdist.sh}";
      buildEditable = "${./scripts/build-editable.py}";
      makeVenv = "${./scripts/make-venv.py}";
    };
    # The planner reads the `pyproject.toml` of each tree below, and resolves
    # the backend that the tree asks for against the same lock file.
    editables = pkgs.lib.mapAttrs (_: spec: {
      source = "${spec.src}";
      inherit (spec) root;
    }) editables;
  };
in
derivation {
  # The name says what this derivation is. The root that it submits is named
  # `demo-venv`, and the two no longer have to agree.
  name = "venv-planner";
  system = pkgs.stdenv.hostPlatform.system;
  builder = "${pkgs.bash}/bin/bash";

  requiredSystemFeatures = [ "builder-rpc-v0" ];

  # The submitted object is a derivation, and every derivation ingests as text.
  __contentAddressed = true;
  outputHashMode = "text";
  outputHashAlgo = "sha256";

  NIX_CONFIG = "experimental-features = nix-command ca-derivations dynamic-derivations";

  # **Every store path that the planner names travels in this string.**
  # `builtins.toJSON` keeps the string context of what it serialises, so each
  # `.drv` of the menu, each tool and each script becomes an input source of
  # this derivation. That is what makes them present in the sandbox, and it is
  # what lets `builtins.storePath` and `builtins.appendContext` name them.
  DDRN_MENU = builtins.toJSON menu;

  DDRN_SYSTEM = pkgs.stdenv.hostPlatform.system;
  DDRN_GRAPH_NIX = "${./graph.nix}";
  DDRN_VENV_NAME = "demo-venv";

  PYTHON_VERSION = pkgs.lib.versions.majorMinor pkgs.python3.version;
  # The planner plans for the host that the graph will run on, which is not
  # necessarily the host that runs the planner. Passing the target in keeps
  # that distinction visible.
  TARGET_PLATFORM = "linux_x86_64";
  TARGET_SYS_PLATFORM = "linux";

  args = [
    "-c"
    ''
      set -eu
      export PYTHONPATH=${packagingPath}
      # Nix runs a builder with the *basename* of the builder as `argv[0]`, and
      # CPython derives `sys.prefix` from `argv[0]`. A virtual environment
      # therefore has to be entered through its full path.
      exec ${builtins.storePath nanopynixEnv}/bin/python ${./plan.py}
    ''
  ];
}
