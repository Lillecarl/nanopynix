#!/usr/bin/env bash
# Build a wheel from an sdist with PEP 517, and then install it.
#
# **This node is the reason the graph has to be a graph.** The backend that
# builds this source is another node of the same graph, and the planner chose
# it from the same lock file. A planner that emits one derivation cannot
# express that: the backend would have to exist before the plan runs.
#
# The environment gives:
#   sdist          the source archive, which the fetch of the graph produced
#   backend        the PEP 517 backend, as `module.submodule`
#   backendPath    PYTHONPATH for the backend, from the nodes that built it
#   installerPath  PYTHONPATH for `installer`, from the node that unpacked it
#   out            the output path
# SC2154: graph.nix passes each variable below as an attribute of the
# derivation, so the build environment holds it.
# shellcheck disable=SC2154
set -eu

root="$PWD"
mkdir -p "$root/src" "$root/wheel"

tar -xzf "$sdist" -C "$root/src" --strip-components=1

cd "$root/src"
# The backend runs with the built backend nodes on its path, and with nothing
# else. There is no network here, and no index.
PYTHONPATH="$backendPath" python3 -c '
import importlib
import sys

backend_name, wheel_directory = sys.argv[1], sys.argv[2]
module, _, attribute = backend_name.partition(":")
backend = importlib.import_module(module)
if attribute:
    backend = getattr(backend, attribute)
print(backend.build_wheel(wheel_directory))
' "$backend" "$root/wheel"
cd "$root"

# The wheel that this node built goes through the same installer as one that
# the lock file named, so both give the same shape of output.
for built in "$root"/wheel/*.whl; do
  PYTHONPATH="$installerPath" python3 -m installer --prefix "$out" "$built"
done
