# A Python environment, built from a lock file, with no PEP logic in Nix.
#
# The Nix half does two things, and neither one knows what a wheel is:
#
#   1. Read the lock file, and make a `fetchurl` derivation for every artefact
#      it names. Nix instantiates all of them and downloads none of them.
#   2. Hand the whole list to the planner as a menu.
#
# The Python half does the work that a Nix expression cannot do without a
# reimplementation of the packaging PEPs: it evaluates the environment markers
# of PEP 508, it ranks the wheel tags of PEP 425 and PEP 600, and it picks the
# one wheel of each package that this host can use. `packaging` is the
# reference implementation of all three, and it runs here unchanged.
{
  lib,
  bash,
  coreutils,
  unzip,
  fetchurl,
  python3,
  ddrn,
}:

let
  lock = builtins.fromJSON (builtins.readFile ./lock.json);

  # One `fetchurl` per artefact. `fetchurl` is a fixed-output derivation, so
  # instantiating it costs nothing and downloads nothing.
  wheelDrv =
    entry:
    fetchurl {
      inherit (entry) url;
      hash = "sha256:${entry.sha256}";
      name = entry.filename;
    };

  # `packaging` is what the planner needs, and only the planner needs it. The
  # environment that comes out carries no trace of it.
  #
  # It goes on `pythonPath` rather than into a `withPackages` interpreter. See
  # `mkPlanner` in `ddrn/nix/planner.nix` for why a wrapper cannot work here.
  plannerPython = python3.withPackages (ps: [ ps.packaging ]);
in
ddrn.mkPlanner {
  name = "demo-venv";
  plan = ./plan.py;
  pythonPath = [ "${plannerPython}/${python3.sitePackages}" ];

  tools = {
    inherit bash coreutils unzip;
    python = python3;
  };

  env = {
    PYTHON_VERSION = lib.versions.majorMinor python3.version;
    # The planner plans for the host that the derivation will run on, which is
    # not necessarily the host that runs the planner. Passing the target in
    # keeps that distinction visible.
    TARGET_PLATFORM = "linux_x86_64";
    TARGET_SYS_PLATFORM = "linux";
  };

  candidates = map (entry: {
    drv = wheelDrv entry;
    name = entry.filename;
    meta = {
      inherit (entry) package version filename;
      marker = entry.marker or null;
    };
  }) lock.wheels;
}
