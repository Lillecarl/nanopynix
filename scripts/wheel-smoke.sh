#!/usr/bin/env bash
#
# Load the wheel on a distribution that has the target glibc, and evaluate a
# Nix expression with it.
#
# `auditwheel` reads the versioned symbols of each object and writes a
# `manylinux` tag that those symbols support. That is a measurement of the
# files, and it is not a run. It cannot see a library that the bundle left out,
# a `$ORIGIN` that points at nothing, or a collector that does not start. Only
# a load on a machine with that glibc shows those.
#
# The default image is Rocky Linux 9, whose glibc is 2.34 exactly, which is the
# floor that `nix/zig-stdenv.nix` targets. A newer image proves less: it
# satisfies a 2.34 requirement whatever the wheel really needs.
#
# The interpreter comes from `uv`, and not from the distribution. Rocky 9 ships
# Python 3.9, and the extension is built for CPython 3.14. `uv` installs a
# python-build-standalone interpreter, which is an ordinary manylinux CPython
# and not a Nix one -- so nothing of this repository is in the container except
# the wheel.
#
# **A distribution, and not any machine with a Python.** A `manylinux` wheel
# bundles what it needs, less the 24 libraries that the policy says the system
# supplies. `libz.so.1` is one of them, so `auditwheel` leaves it out on
# purpose. A NixOS host has no `libz.so.1` on the default search path, and the
# import there stops with `libz.so.1: cannot open shared object file`. That
# reads like a fault of the wheel and it is not: the same wheel imports in the
# container, and an x86-64 wheel fails the same way on the same host.
#
# Usage:
#   nix build --file . nanopynixWheel --out-link result-wheel
#   scripts/wheel-smoke.sh result-wheel
#
# The second argument names another image:
#   scripts/wheel-smoke.sh result-wheel docker.io/library/debian:12
#
# **This runs the wheel of the machine it runs on.** For a wheel of another
# architecture, use `scripts/wheel-inspect.sh`, which reads the files and needs
# no interpreter of that architecture. A container of a foreign architecture
# does not answer: this host registers `aarch64-linux` binfmt with the `P` flag
# and not `F`, so the kernel looks for the emulator inside the mount namespace
# of the container. Mounting the emulator there is not sufficient either --
# measured, and qemu then exits 255 with no output.

set -euo pipefail

wheel_dir=${1:?usage: wheel-smoke.sh <directory holding the wheel> [image]}
image=${2:-docker.io/library/rockylinux:9}
python_version=${WHEEL_SMOKE_PYTHON:-3.14}

wheel_dir=$(readlink -f "$wheel_dir")
if ! compgen -G "$wheel_dir/*.whl" >/dev/null; then
    echo "wheel-smoke: no wheel in $wheel_dir" >&2
    exit 1
fi

runtime=${WHEEL_SMOKE_RUNTIME:-podman}

# Name the platform, and do not let the local image cache pick it. A pull of
# the same tag for another architecture stays in the cache, and the next run
# then starts that one and stops with `exec container process (missing dynamic
# library?)`, which does not name the cause.
case "$(uname -m)" in
x86_64) platform=linux/amd64 ;;
aarch64) platform=linux/arm64 ;;
*)
    echo "wheel-smoke: no platform known for $(uname -m)" >&2
    exit 1
    ;;
esac

work_dir=$(mktemp -d -t nanopynix-wheel-smoke-XXXXXX)
trap 'rm -rf "$work_dir"' EXIT

# The test is a here-document rather than a file in `scripts/`, because it
# imports `nanopynix_bindings`, which resolves in the container alone. A file
# would join the ruff and pyright filesets, and both would report on an import
# that cannot resolve in this checkout.
cat >"$work_dir/smoke.py" <<'PYTHON'
import platform
import sys

print("python  :", sys.version.split()[0], platform.machine())
print("libc    :", platform.libc_ver())

from nanopynix_bindings import expr, store, util

print("import  : ok")

# `load_config=False`, so the wheel reads no `nix.conf` of the container.
util.init_libstore(load_config=False)
print("libstore: ok")

# `dummy://` needs no daemon, no `/nix/store` and no network.
expr.init_libexpr()
state = expr.EvalState(store.open_store("dummy://"), [], None, {}, {})
print("store   : ok")

value = state.eval_string("1 + 1", "<smoke>")
value.force()
assert value.as_int() == 2, value.as_int()
print("arith   : 1 + 1 =", value.as_int())

value = state.eval_string('builtins.concatStringsSep "-" ["nix" "on" "pypi"]', "<smoke>")
value.force()
assert value.as_string() == "nix-on-pypi", value.as_string()
print("strings :", value.as_string())

info = util.build_info()
print("nix     :", info["nix_version"])
assert info["capabilities"]["boehm_gc"], "the wheel carries no collector"
print("gc      : boehm_gc =", info["capabilities"]["boehm_gc"])

# Every store backend registers itself with a file-scope static object, so a
# backend that did not build is absent and nothing else says so. `s3://` is the
# one that needs the 13 AWS CRT libraries, and it is the reason they are in the
# closure at all.
import json

schemes = set()
for entry in json.loads(store.list_store_types_json()).values():
    schemes.update(entry["uri-schemes"])
print("stores  :", " ".join(sorted(schemes)))
# `unix` is the daemon, and there is no `daemon` scheme.
for required in ("s3", "ssh", "ssh-ng", "unix", "file", "http", "https", "local", "dummy"):
    assert required in schemes, f"the wheel carries no {required}:// store"
print("s3      : registered")

# A deep force over a large tree runs the collector, which is the part of the
# closure that a plain import never reaches.
value = state.eval_string(
    "let f = n: if n == 0 then [] else [ { v = n; } ] ++ f (n - 1); in f 2000",
    "<smoke>",
)
value.force_deep()
assert value.list_length() == 2000, value.list_length()
print("gc      : forced", value.list_length(), "attribute sets")

# ── The two checks that only a failing path reaches ──────────────────────
#
# Everything above asserts that the wheel *works*. Both defects of issue #112
# left every one of those checks green, because both need an error or a
# `dynamic_cast` to show. A `catch` and a `dynamic_cast` across two shared
# objects are exactly what a bundled wheel adds, so this is the part of the
# wheel that the ordinary test suite cannot cover.

# 1. An error keeps its class. The extension names each Nix exception in a
#    `catch` clause, and `libnixexpr.so` throws it, so the two objects have to
#    agree about the type. When they do not, every clause misses and nanobind
#    reports `SystemError: exception could not be translated`.
from nanopynix_bindings import errors

try:
    state.eval_string('throw "boom"', "<smoke>").force()
    raise AssertionError("throw did not raise")
except errors.ThrownError as caught:
    print("errors  : ThrownError, and the message survived:", "boom" in str(caught))
except SystemError as caught:  # noqa: F841 -- named to make the report readable
    raise AssertionError(
        "the wheel cannot translate a Nix exception -- see issue #112. "
        "Type information is hidden, so no catch clause matches."
    ) from caught

# 2. `dynamic_cast` still finds a base class. **This one fails silently.** The
#    store below is a LocalStore, so `find_roots` has to work. When the cast
#    returns null the binding reports "store does not support garbage
#    collection", which reads like a true answer about a limited store.
import tempfile

root = tempfile.mkdtemp()
local = store.open_store(f"local?root={root}")
try:
    local.find_roots()
except Exception as caught:
    raise AssertionError(
        f"dynamic_cast across the bundled objects failed -- see issue #112: {caught}"
    ) from caught
print("casts   : find_roots on a local store, ok")

print("RESULT  : ok")
PYTHON

echo "wheel-smoke: $image ($platform), CPython $python_version"
"$runtime" run --rm --network=bridge --platform "$platform" \
    -v "$wheel_dir":/wheel:ro,z \
    -v "$work_dir/smoke.py":/smoke.py:ro,z \
    -e "PYTHON_VERSION=$python_version" \
    "$image" \
    sh -c '
        set -e
        echo "distro  : $(. /etc/os-release && echo "$PRETTY_NAME")"
        echo "glibc   : $(ldd --version | head -1 | sed "s/.*) //")"
        curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1
        export PATH=/root/.local/bin:$PATH
        uv venv --python "$PYTHON_VERSION" /venv >/dev/null 2>&1
        # The wheel file, by path, and not the project by name. A publishable
        # build carries the Nix version in its name -- `nanopynix-bindings-nix2-34`
        # -- so a name written here would be wrong for every build but one.
        uv pip install --python /venv/bin/python --no-index /wheel/*.whl >/dev/null
        /venv/bin/python /smoke.py
    '
