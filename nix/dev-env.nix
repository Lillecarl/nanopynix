# The interactive development environment: one venv containing every project
# in this repo as an editable install, so an edit is live with no rebuild.
#
# The whole thing is a `mkVirtualEnv` spec. What it replaces was a hand-written
# `python.withPackages` list that had to append each project's `.dependencies`
# alongside the project itself, because nixpkgs' Python environments keep only
# importable modules and so drop an application *together with everything it
# propagates*. A venv has no such rule, so the dependency graph is not restated
# here -- and a name that goes missing from a pyproject.toml now fails
# resolution instead of silently falling back to a store copy.
#
# Extras are chosen here rather than on the packages themselves, which is why
# the same `pynix` that ships as a release application without its test extra
# appears here with it (see nix/py-packages.nix).
#
# Exported as `pynixDevEnv` so other repos can drop a live, hot-reloading
# `pynix` into their own devShell or direnv without rebuilding on every
# edit here: each project's editable root is this checkout's own absolute
# path, so the generated path hooks resolve straight back here at import time
# with nothing for the consumer to export. Does not include nanopynix's own
# devtools (pyright/ruff/...) -- see nix/shell.nix for the full shell.
{
  editablePythonSet,
  # Merged over the spec below, replacing the entry for any project it names.
  # The full nanopynix shell uses it to turn on pynix's `docs` extra, which
  # the exported `pynixDevEnv` has no use for.
  extraSpec ? { },
}:

editablePythonSet.mkVirtualEnv "nanopynix-dev-env" (
  {
    nanopynix = [ "test" ];
    nanopynix-helpers = [ "test" ];
    # The CLI layer, which issue #222 moved out of `pynix`. Already in the
    # venv as a dependency of `pynix`; named here for its `test` extra, so
    # `pytest libpynix` runs from the dev shell.
    libpynix = [ "test" ];
    pynix = [ "test" ];
    # The language server, which `pynix` mounts through an optional import.
    # Named here so the dev shell has the `lsp` subcommand and can run its
    # suite; a release `pynix` that leaves it out still works, without that
    # subcommand. Issue #107.
    pynix-lsp = [ "test" ];
    pytest-agent = [ ];
    # Already in the venv as a dependency of `nanopynix`; named here for its
    # `test` extra, which is what puts `greeter-proto`, `asyncssh` and `rich`
    # in reach so `pytest grpclib-transports` runs from the dev shell.
    grpclib-transports = [ "test" ];
    # The daemon proxy that the merge of issue #131 brought in, editable like
    # every other project here. `nix-daemon-protocol` arrives as its
    # dependency, so it needs no entry of its own. The `test` extra is what
    # makes `pytest pynixd` run from the dev shell.
    pynixd = [ "test" ];
    # The two dependencies of `completion-spike`. That subproject is a nixpkgs
    # `buildPythonApplication` rather than a package of this set, so it cannot
    # be named here -- but `pyright` and `pytest` run over its tree from the
    # dev shell, and without these two the dev shell would report errors that
    # the CI gate does not. See nix/checks.nix, which names the same two for
    # the same reason.
    cyclopts = [ ];
    pexpect = [ ];
  }
  // extraSpec
)
