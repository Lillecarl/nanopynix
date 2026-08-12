"""Fail the wheel build when the wheel is built wrong but still installs.

`nix/wheel.nix` runs this on the unpacked wheel, after `auditwheel repair`.
Every gate here answers one question that a successful build does not.

**What is already gated, and is therefore not here.** `auditwheel repair` reads
the versioned symbols of every object and refuses a tag that the objects do not
support. Measured: a repair of this wheel to `manylinux_2_28` ends with "cannot
repair ... because of the presence of too-recent versioned symbols". So the
glibc floor needs no gate of its own, and a gcc build that raises the floor to
2.38 stops the build already.

The four gates below cover what stays silent:

1. **One C++ runtime.** Issue #112: a build that links the C++ standard library
   statically into every shared object gives the wheel one runtime per library,
   and one library then destroys a static object of another.
   `nix/cxx-runtime.nix` gives them one shared runtime instead. A build that loses that flag links, installs, imports,
   evaluates `1 + 1`, and then ends the process on the first error.
2. **No C++ standard library of the host.** A C++ object from outside the
   closure can meet the glibc floor and still bring a second standard library.
   No object of this wheel may name one in `DT_NEEDED`.
3. **A payload ceiling.** The payload was 90 MiB before the trim of issue #111.
   Nothing reports a return to that size, because a large wheel installs.
4. **Each store backend.** Every backend registers itself with a file-scope
   static object, so a backend that did not build is absent and nothing says
   so. `scripts/wheel-smoke.sh` asks the built wheel for its schemes, and that
   is the stronger check -- but it needs a container, a network and an
   interpreter of the wheel's own architecture. This gate runs inside the Nix
   build, on both architectures, and it is the only backend check that the
   aarch64 wheel gets.
5. **The stable ABI, as asked for.** `STABLE_ABI` is a positional argument of
   `nanobind_add_module`, so dropping it changes nothing that fails: the wheel
   builds, installs and imports, on the one CPython that built it. The suffix
   of the extension is what says which build ran.
"""

from __future__ import annotations

import struct
import sys
from dataclasses import dataclass
from pathlib import Path

# ELF64, which both targets are. `nix/cxx-stdenv.nix` builds `x86_64-linux-gnu`
# and `aarch64-linux-gnu`, and there is no 32-bit wheel.
#
# **This reads the file, and does not call `readelf`.** pyelftools would do the
# same job, and it carries no type information, so `nix/checks.nix` reports ten
# errors on an import of it. `readelf` would need its output parsed as text.
# The three structures below are fixed by the ELF specification and have not
# changed since 1999.
ELF_MAGIC = b"\x7fELF"
ELF_CLASS_64 = 2
ELF_DATA_LSB = 1
SECTION_HEADER_SIZE = 64
SYMBOL_SIZE = 24
DYNAMIC_SIZE = 16
SHT_DYNSYM = 11
SHT_DYNAMIC = 6
SHN_UNDEF = 0
DT_NULL = 0
DT_NEEDED = 1

# A definition of any of these means "this object carries a C++ runtime".
# Measured on a correct wheel: `libnanopynixcxx.so.1` defines 15 of them, and
# every other object of the 49 defines none.
#
# `__cxa_atexit` and `__cxa_thread_atexit_impl` are deliberately absent. Those
# two are glibc, and libcrypto, libgc and libs2n reference them while holding
# no C++ at all.
CXX_RUNTIME_SYMBOLS = frozenset(
    {
        "__cxa_throw",
        "__cxa_begin_catch",
        "__cxa_end_catch",
        "__gxx_personality_v0",
    }
)

# The type information classes of the C++ ABI. A second copy of these is what
# made `abort "x"` reach Python as `SystemError`: the walk over base classes
# opens with a `dynamic_cast` over the type information object itself, and that
# cast needs one `__si_class_type_info` for the process.
CXX_RUNTIME_PREFIX = "_ZTVN10__cxxabiv1"

# A C++ standard library that is not the one runtime of this closure. A wheel
# that names one of these in `DT_NEEDED` takes a second C++ runtime from the
# host, which is the defect of issue #112 with a different origin.
#
# **`DT_NEEDED`, and not the `__cxx11` tag of libstdc++.** That tag was the
# first gate written here, and it is too weak: a measurement on a gcc-built
# object that returns a `std::string` found no `__cxx11` in `.dynsym` at all,
# because gcc put the tag in the mangled name as `B5cxx11` and inlined the
# constructor. The same object named `libstdc++.so.6` in `DT_NEEDED`, which no
# amount of inlining removes. A libstdc++ linked statically has no `DT_NEEDED`
# either, and the runtime gate above catches that one: a static libstdc++
# brings libsupc++, which defines `__cxa_throw`.
FOREIGN_CXX_RUNTIMES = frozenset(
    {
        "libstdc++.so.6",
        "libc++.so.1",
        "libc++abi.so.1",
    }
)

# Each one registers a scheme with a file-scope static object. A name here is
# the mangled length prefix and the class, so `SSHStore` cannot match inside
# `LegacySSHStore`.
STORE_BACKENDS = (
    "LocalStore",
    "UDSRemoteStore",
    "SSHStore",
    "LegacySSHStore",
    "MountedSSHStore",
    "S3BinaryCacheStore",
    "HttpBinaryCacheStore",
    "LocalBinaryCacheStore",
    "LocalOverlayStore",
    "DummyStore",
)

# The suffix that a stable ABI extension carries. nanobind writes
# `_ext.cpython-314-x86_64-linux-gnu.so` for an ordinary build and
# `_ext.abi3.so` for this one.
STABLE_ABI_SUFFIX = ".abi3.so"

ARGUMENT_COUNT = 5


@dataclass(frozen=True)
class Elf:
    """What the gates read out of one shared object."""

    # The name of each dynamic symbol, and whether this object defines it.
    symbols: tuple[tuple[str, bool], ...] = ()
    # The `DT_NEEDED` entries, in order.
    needed: tuple[str, ...] = ()


def name_at(strings: bytes, offset: int) -> str:
    """The name at an offset into a string table. Each one ends with a zero."""
    return strings[offset : strings.index(b"\0", offset)].decode()


def read_symbols(data: bytes, order: str, strings: bytes, offset: int, size: int) -> list[tuple[str, bool]]:
    """One `.dynsym` section: the name of each symbol, and whether it is defined."""
    symbols: list[tuple[str, bool]] = []
    for position in range(offset, offset + size, SYMBOL_SIZE):
        name_offset, _, _, section = struct.unpack_from(f"{order}IBBH", data, position)
        symbols.append((name_at(strings, name_offset), section != SHN_UNDEF))
    return symbols


def read_needed(data: bytes, order: str, strings: bytes, offset: int, size: int) -> list[str]:
    """One `.dynamic` section: the `DT_NEEDED` entries, in order."""
    needed: list[str] = []
    for position in range(offset, offset + size, DYNAMIC_SIZE):
        tag, value = struct.unpack_from(f"{order}qQ", data, position)
        if tag == DT_NULL:
            break
        if tag == DT_NEEDED:
            needed.append(name_at(strings, value))
    return needed


def read_elf(path: Path) -> Elf:
    """Read the dynamic symbols and the needed libraries of an ELF64 object.

    An empty result means the file is not an ELF64 object, which is what every
    Python file of the wheel is.
    """
    data = path.read_bytes()
    if data[:4] != ELF_MAGIC or data[4] != ELF_CLASS_64:
        return Elf()
    order = "<" if data[5] == ELF_DATA_LSB else ">"

    # The section header table: its offset, the size of an entry, and the
    # count. `e_shoff` is at 0x28, and `e_shentsize` and `e_shnum` at 0x3a.
    (table,) = struct.unpack_from(f"{order}Q", data, 0x28)
    entry_size, count = struct.unpack_from(f"{order}HH", data, 0x3A)
    if entry_size != SECTION_HEADER_SIZE:
        return Elf()

    # Annotated, because `struct.unpack_from` gives `tuple[Any, ...]` and every
    # value taken out of it is then unknown to the type checker.
    headers: list[tuple[int, ...]] = [
        struct.unpack_from(f"{order}IIQQQQIIQQ", data, table + index * entry_size) for index in range(count)
    ]

    symbols: list[tuple[str, bool]] = []
    needed: list[str] = []

    for _, kind, _, _, offset, size, link, *_ in headers:
        if kind not in (SHT_DYNSYM, SHT_DYNAMIC):
            continue
        # `sh_link` of either section names the string table that holds its
        # names, so this takes the right one even in a file with several.
        strings_offset, strings_size = headers[link][4], headers[link][5]
        strings = data[strings_offset : strings_offset + strings_size]
        if kind == SHT_DYNSYM:
            symbols += read_symbols(data, order, strings, offset, size)
        else:
            needed += read_needed(data, order, strings, offset, size)

    return Elf(symbols=tuple(symbols), needed=tuple(needed))


def check_one_cxx_runtime(objects: dict[Path, Elf], runtime: str) -> list[str]:
    """Exactly one object may define the C++ runtime, and it must be that one."""
    definers = {
        path
        for path, elf in objects.items()
        for name, defined in elf.symbols
        if defined and (name in CXX_RUNTIME_SYMBOLS or name.startswith(CXX_RUNTIME_PREFIX))
    }
    wrong = sorted(path.name for path in definers if not path.name.startswith(runtime))
    if wrong:
        return [
            "these objects carry a C++ runtime of their own:",
            *(f"    {name}" for name in wrong),
            f"  Only `{runtime}*.so*` may. Read nix/cxx-runtime.nix and issue #112:",
            "  a second runtime means one library destroys a static object of another,",
            "  and the wheel imports and evaluates before it ends the process.",
        ]
    if not definers:
        return [
            f"no object defines the C++ runtime, and `{runtime}*.so*` should.",
            "  The runtime did not reach the wheel, so nothing resolves `__cxa_throw`.",
        ]
    return []


def check_no_foreign_cxx_runtime(objects: dict[Path, Elf]) -> list[str]:
    """No object may take a second C++ standard library from the host."""
    found = sorted(
        (path.name, library)
        for path, elf in objects.items()
        for library in elf.needed
        if library in FOREIGN_CXX_RUNTIMES
    )
    if found:
        return [
            "these objects name a C++ standard library of the host:",
            *(f"    {name} needs {library}" for name, library in found),
            "  The whole closure takes one C++ runtime, from nix/cxx-stdenv.nix, and",
            "  nix/cxx-runtime.nix links one copy of it into the wheel. A second",
            "  standard library in the process is the defect of issue #112.",
        ]
    return []


def check_payload(unpacked: Path, ceiling: int) -> list[str]:
    """The unpacked wheel must stay under the ceiling."""
    total = sum(item.stat().st_size for item in unpacked.rglob("*") if item.is_file())
    if total > ceiling:
        return [
            f"the payload is {total / 1024 / 1024:.1f} MiB, over the ceiling of {ceiling / 1024 / 1024:.1f} MiB.",
            "  Read nix/nix-closure.nix for the trim of issue #111. A debug build, an",
            "  object that lost its strip, or a library that joined the closure each",
            "  gives this, and each one builds a wheel that installs.",
        ]
    sys.stdout.write(f"wheel-gates: payload {total / 1024 / 1024:.1f} MiB of {ceiling / 1024 / 1024:.1f} MiB\n")
    return []


def check_store_backends(objects: dict[Path, Elf]) -> list[str]:
    """`libnixstore` must still hold every store backend."""
    store = [path for path in objects if path.name.startswith("libnixstore")]
    if len(store) != 1:
        return [f"the wheel holds {len(store)} objects called `libnixstore*`, and it needs one."]

    names = {name for name, _ in objects[store[0]].symbols}
    missing = [backend for backend in STORE_BACKENDS if not any(f"{len(backend)}{backend}" in name for name in names)]
    if missing:
        return [
            "these store backends are not in `libnixstore`:",
            *(f"    {backend}" for backend in missing),
            "  Each one registers its scheme with a file-scope static object, so a",
            "  backend that did not build is absent and raises no error until a user",
            "  opens that scheme.",
        ]
    return []


def check_stable_abi(unpacked: Path, wanted: bool) -> list[str]:
    """The extension carries the stable ABI, or it does not, as asked."""
    extensions = sorted(path.name for path in unpacked.rglob("_ext*.so"))
    if len(extensions) != 1:
        return [f"the wheel holds {len(extensions)} files called `_ext*.so`, and it needs one."]

    found = extensions[0].endswith(STABLE_ABI_SUFFIX)
    if wanted and not found:
        return [
            f"the extension is `{extensions[0]}`, and the stable ABI was asked for.",
            "  `STABLE_ABI` did not reach `nanobind_add_module`. The wheel then serves",
            "  one CPython minor version, and PyPI needs one wheel for each of them.",
        ]
    if not wanted and found:
        return [
            f"the extension is `{extensions[0]}`, and the stable ABI was not asked for.",
            "  A build that Nix consumes serves one interpreter, and the stable ABI",
            "  costs speed at every crossing of the boundary for nothing.",
        ]
    return []


def main() -> int:
    if len(sys.argv) != ARGUMENT_COUNT:
        sys.stderr.write(
            "usage: wheel-gates.py <unpacked wheel> <payload ceiling in bytes> <runtime soname> <stable abi: 1 or 0>\n"
        )
        return 2

    unpacked = Path(sys.argv[1])
    ceiling = int(sys.argv[2])
    # `libnanopynixcxx.so.1` in the closure, and `libnanopynixcxx-16969e45.so.1`
    # here: `auditwheel` puts 8 hexadecimal characters before the `.so` of every
    # library that it copies. So this is a prefix, and not a name.
    runtime = sys.argv[3].split(".so")[0]
    stable_abi = sys.argv[4] == "1"

    paths = sorted(path for path in unpacked.rglob("*") if path.is_file() and ".so" in path.name)
    if not paths:
        sys.stderr.write("wheel-gates: the wheel holds no shared object.\n")
        return 1

    objects = {path: read_elf(path) for path in paths}

    failures = [
        *check_one_cxx_runtime(objects, runtime),
        *check_no_foreign_cxx_runtime(objects),
        *check_payload(unpacked, ceiling),
        *check_store_backends(objects),
        *check_stable_abi(unpacked, stable_abi),
    ]
    if failures:
        sys.stderr.write("wheel-gates: " + "\n".join(failures) + "\n")
        return 1

    sys.stdout.write(
        f"wheel-gates: {len(paths)} objects, one C++ runtime, no C++ library of the host, "
        f"{len(STORE_BACKENDS)} store backends, "
        f"{'stable' if stable_abi else 'version-locked'} ABI\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
