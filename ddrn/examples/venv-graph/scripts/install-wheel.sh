#!/usr/bin/env bash
# Install one wheel, the way PEP 376 says to.
#
# **`unzip` is not an install.** A wheel unpacked with `unzip` has no `RECORD`,
# no console script, and no `.dist-info` that an installer wrote. `pypa/installer`
# is the reference implementation, and `pyproject.nix` uses it for the same
# reason (`build/hooks/pypa-install-hook`).
#
# `installer` is another node of this same graph. The planner resolved it from
# the same lock file that gave it every other artefact, so nothing here comes
# from outside the plan.
#
# The environment gives:
#   wheel          the `.whl` file, which the fetch of the graph produced
#   wheelName      the name that the index gave that file
#   installerPath  PYTHONPATH for `installer`, from the node that unpacked it
#   out            the output path
# SC2154: graph.nix passes each variable below as an attribute of the
# derivation, so the build environment holds it.
# shellcheck disable=SC2154
set -eu

# **A store path is not a wheel file name.** `installer` reads the name of the
# file to learn which `.dist-info` directory the wheel must carry, and a store
# path puts a hash in front of that name. The install then fails with "Wheel
# .dist-info directory doesn't match wheel filename". So the wheel goes under
# its own name first. `pyproject.nix` and nixpkgs install from a `dist`
# directory for the same reason.
cp "$wheel" "./$wheelName"

# The interpreter that runs the install is the one whose shebang each console
# script gets. `make-venv.py` rewrites that shebang to the `bin/python` of the
# environment, which is what makes a script find the packages beside it.
PYTHONPATH="$installerPath" python3 -m installer --prefix "$out" "./$wheelName"
