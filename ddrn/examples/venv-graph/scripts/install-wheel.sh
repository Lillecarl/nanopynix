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
#
# A wheel that carries a binary also gives:
#   autoPatchelf   the `auto-patchelf` of nixpkgs
#   patchelfLibs   the directories to search for a shared library
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

# **A wheel that carries a binary is built for a different Linux.** A manylinux
# wheel names `/lib64/ld-linux-x86-64.so.2` as its ELF interpreter and gives no
# RPATH, and neither the loader at that path nor `libstdc++.so.6` exists on
# this system. `auto-patchelf` of nixpkgs writes the loader of this system into
# each binary, and adds an RPATH that names each library that the binary needs.
#
# `pyproject.nix` does the same, with `autoPatchelfHook` in `nativeBuildInputs`
# (`build/hacks/default.nix`). This node calls the same program directly,
# because a graph node is a plain derivation and runs no setup hook.
#
# **The planner decides which node needs this.** A wheel whose platform tag is
# `any` carries no binary, so its node gets no `autoPatchelf` and takes no
# dependency on a compiler library.
if [ -n "${autoPatchelf-}" ]; then
  # `--libs` takes one directory for each argument, so the string that
  # `graph.nix` gives has to become a list again.
  read -r -a libraryPath <<<"$patchelfLibs"
  "$autoPatchelf" --paths "$out" --libs "${libraryPath[@]}"
fi
