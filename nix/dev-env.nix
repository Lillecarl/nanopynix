# The interactive development environment: one venv containing every project
# in this repo as an editable install, so an edit is live with no rebuild.
#
# The whole thing is a `mkVirtualEnv` spec now. What it replaces was a
# hand-written `python.withPackages` list that had to append each project's
# `.dependencies` alongside the project itself, because nixpkgs' Python
# environments keep only importable modules and so drop an application
# *together with everything it propagates*. That is not a rule a venv has, so
# the dependency graph no longer has to be restated here -- and a name that
# goes missing from a pyproject.toml now fails resolution instead of silently
# falling back to a store copy.
#
# Extras are chosen here rather than on the packages themselves, which is why
# the same `pynix` that ships as a release application without its test extra
# appears here with it (see nix/py-packages.nix).
{
  editablePythonSet,
  # Merged over the venv spec below, replacing the entry for any project it
  # names. The full nanopynix shell uses it to turn on pynix's `docs` extra,
  # which the exported `pynixDevEnv` has no use for.
  extraSpec ? { },
}:
{
  # Exported so other repos can drop a live, hot-reloading `pynix`/`ekn` into
  # their own devShell or direnv without rebuilding on every edit here: each
  # project's editable root is this checkout's own absolute path, so the
  # generated path hooks resolve straight back here at import time with
  # nothing for the consumer to export. Does not include nanopynix's devtools
  # (pyright/ruff/...) -- see nix/shell.nix for the full interactive shell.
  pythonEnv = editablePythonSet.mkVirtualEnv "nanopynix-dev-env" (
    {
      nanopynix = [ "test" ];
      nanopynix-helpers = [ "test" ];
      # `ekn` as well as `test`: pynix declares an optional dependency on ekn,
      # and enabling it here is what lets the two cross-import each other's
      # modules from one venv -- which a real application-to-application
      # dependency cannot do without a derivation cycle.
      pynix = [
        "test"
        "ekn"
      ];
      ekn = [ ];
      pytest-agent = [ ];
    }
    // extraSpec
  );
}
