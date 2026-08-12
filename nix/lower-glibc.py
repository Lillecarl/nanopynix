"""Lower the glibc floor of an ELF64 shared object, in place.

**The floor of the wheel is gratuitous, and this file removes it.** A wheel
takes the highest glibc version node of every object it carries, and the stdenv
of nixpkgs puts that node at `GLIBC_2.38` for every C++ object. Measured over
the 105 real objects of the nix-store closure on 2026-08-12: 39 of them sat at
`GLIBC_2.38`, and not one used a capability of glibc 2.38.

Three causes, and this file removes the first two:

1. **`__isoc23_strtol` and its family.** `features.h` of glibc defines
   `_ISOC23_SOURCE` from `_GNU_SOURCE`, which g++ always defines, which sets
   `__GLIBC_USE_C23_STRTOL`, which makes `stdlib.h` redirect `strtol` to
   `__isoc23_strtol@GLIBC_2.38`. The function is the same function. C23 added a
   `0b` prefix for base 0 and base 2, and every library here predates C23, so
   none can depend on that.

2. **`fmod@GLIBC_2.38`.** glibc 2.38 gave the same function a second version
   node. The old node still defines it.

3. **`arc4random`, `arc4random_buf`, `strlcpy` and `strlcat`.** These are
   genuinely new, so a rename cannot reach them. `nix/arc4random-compat.c`
   supplies the first two, and `nix/nix-closure.nix` builds curl without GSSAPI,
   which removes krb5 and with it the other two.

**An ABI marker is the fourth cause, and it is a failure and not a rewrite.** A
node named `GLIBC_ABI_*` carries no symbol. It is a demand for an
implementation, and the compiler flag that emitted the relocations behind it is
what has to change. `GLIBC_ABI_GNU2_TLS` is the one that this closure met: GCC
15 made `-mtls-dialect=gnu2` the default on x86-64, and glibc 2.41 is the first
release that answers the marker. `nix/cxx-stdenv.nix` holds the correction, and
this file reports the marker so that a later compiler cannot put it back
quietly.

**The rename needs no new string.** Every redirected name is `__isoc23_` plus
the real name, and the real name is therefore already in `.dynstr` as a suffix
of it. So the edit is a nine-byte bump of `st_name`, and no section resizes and
no offset in the file moves.

The version index then moves to the base node of the architecture, which defines
every name this file touches. A node that loses every referent is renamed to
that one as well, because an entry left in `.gnu.version_r` keeps the floor high
with no symbol behind it.

**The base node is a property of the architecture, and the two differ.** x86-64
uses `GLIBC_2.2.5` and aarch64 uses `GLIBC_2.17`, because glibc added aarch64 in
2.17. This file therefore reads `e_machine` and never assumes one of them.
Measured on the aarch64 build: a hard-coded `GLIBC_2.2.5` is absent from every
aarch64 object, so the rewrite found no base node and lowered nothing at all.
Verified against the aarch64 `libc.so.6` and `libm.so.6` of glibc 2.42: every
`__isoc23_` target and each of `fmod`, `fmodf` and `fmodl` is defined at
`GLIBC_2.17`.

**This runs at the fixup of each package, and not on the wheel.** `auditwheel`
reads each library through the RPATH of the extension, and those paths are in
the Nix store and read-only. It also refuses a tag before it repairs, so a
rewrite after the repair would never be reached. `nix/cxx-stdenv.nix` therefore
installs this as a setup hook, and every object is lowered by the build that
made it.

The exit status is the gate: a symbol above the target that this file cannot
rename fails the build that installed it, and names the object and the symbol.
"""

from __future__ import annotations

import argparse
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

ELF_MAGIC = b"\x7fELF"
ELFCLASS64 = 2
# The size of an ELF64 header. A file shorter than this holds no section table,
# whatever its first four bytes say.
EHDR_SIZE = 0x40
SHT_DYNSYM = 11
SYM_SIZE = 24
SHDR_SIZE = 64
ISOC23 = b"__isoc23_"

# The node that defines every name this file moves a symbol to, by `e_machine`.
# The head of this file gives the measurement behind each entry. An architecture
# that is absent here is a failure, and never a silent pass.
EM_X86_64 = 62
EM_AARCH64 = 183
BASE_NODES = {
    EM_X86_64: b"GLIBC_2.2.5",
    EM_AARCH64: b"GLIBC_2.17",
}

# A name that is not an `__isoc23_` rename but that the base node still defines.
# glibc 2.38 gave `fmod` a second node, and the first one remains.
RENAMELESS = frozenset({b"fmod", b"fmodf", b"fmodl"})

# The compiler flag that removes each ABI marker, by node name. The head of this
# file says why a marker is a failure and not a rewrite.
ABI_MARKERS = {
    b"GLIBC_ABI_GNU2_TLS": (
        "glibc 2.41 answers this marker. Build with `-mtls-dialect=gnu`, which "
        "is the default of every gcc before 15 on x86-64."
    ),
}


def version_key(node: bytes) -> tuple[int, ...]:
    """Sort key for a `GLIBC_2.34` style node name."""
    return tuple(int(part) for part in node.removeprefix(b"GLIBC_").split(b".") if part.isdigit())


@dataclass
class Section:
    """One entry of the section header table."""

    name_off: int
    kind: int
    offset: int
    size: int
    link: int
    name: bytes = b""


class Elf:
    """The little that this file needs of an ELF64 image, over a bytearray."""

    def __init__(self, data: bytearray) -> None:
        self.data = data
        self.machine: int = struct.unpack_from("<H", data, 0x12)[0]
        shoff: int = struct.unpack_from("<Q", data, 0x28)[0]
        shnum: int = struct.unpack_from("<H", data, 0x3C)[0]
        shstrndx: int = struct.unpack_from("<H", data, 0x3E)[0]
        self.sections = [self._section(shoff + i * SHDR_SIZE) for i in range(shnum)]
        names = self.sections[shstrndx].offset
        for section in self.sections:
            section.name = self.cstr(names + section.name_off)

    def _section(self, at: int) -> Section:
        fields: tuple[int, ...] = struct.unpack_from("<IIQQQQIIQQ", self.data, at)
        return Section(
            name_off=fields[0],
            kind=fields[1],
            offset=fields[4],
            size=fields[5],
            link=fields[6],
        )

    def cstr(self, offset: int) -> bytes:
        return bytes(self.data[offset : self.data.index(b"\0", offset)])

    def find(self, name: bytes) -> Section | None:
        return next((section for section in self.sections if section.name == name), None)


def is_elf64(data: bytes | bytearray) -> bool:
    return len(data) > EHDR_SIZE and data[:4] == ELF_MAGIC and data[4] == ELFCLASS64


def read_version_nodes(elf: Elf, verneed: Section, dynstr_off: int) -> tuple[dict[int, bytes], dict[int, int]]:
    """Map each version index to its node name and to the offset of its entry."""
    names: dict[int, bytes] = {}
    entry_at: dict[int, int] = {}
    need = verneed.offset
    while True:
        _version, count, _file, aux, next_need = struct.unpack_from("<HHIII", elf.data, need)
        at = need + aux
        for _ in range(count):
            _hash, _flags, index, name_off, next_aux = struct.unpack_from("<IHHII", elf.data, at)
            names[index] = elf.cstr(dynstr_off + name_off)
            entry_at[index] = at
            if not next_aux:
                break
            at += next_aux
        if not next_need:
            break
        need += next_need
    return names, entry_at


class Versions(NamedTuple):
    """The version table of one object, read once and passed around whole."""

    # The node name of each version index.
    nodes: dict[int, bytes]
    # The offset of the `Elf64_Vernaux` entry of each version index.
    entry_at: dict[int, int]
    # The index of the base node, which every lowered symbol moves onto.
    base: int
    # The name of that node. It differs by architecture, so it is read from the
    # object and never assumed.
    base_name: bytes


def elf_hash(name: bytes) -> int:
    value = 0
    for byte in name:
        value = (value << 4) + byte
        high = value & 0xF0000000
        if high:
            value ^= high >> 24
        value &= ~high & 0xFFFFFFFF
    return value


def _lower_symbols(
    elf: Elf,
    dynsym: Section,
    versym: Section,
    versions: Versions,
    ceiling: tuple[int, ...],
) -> tuple[int, list[str]]:
    """Rename each redirected symbol, and move it onto the base node."""
    data = elf.data
    dynstr = elf.sections[dynsym.link].offset
    changed = 0
    remaining: list[str] = []

    for i in range(dynsym.size // SYM_SIZE):
        symbol = dynsym.offset + i * SYM_SIZE
        (name_off,) = struct.unpack_from("<I", data, symbol)
        name = elf.cstr(dynstr + name_off)
        at = versym.offset + i * 2
        (raw,) = struct.unpack_from("<H", data, at)
        node = versions.nodes.get(raw & 0x7FFF)

        if node is None or not node.startswith(b"GLIBC_") or version_key(node) <= ceiling:
            continue

        if name.startswith(ISOC23):
            # The real name is the suffix, so the string table already holds it.
            struct.pack_into("<I", data, symbol, name_off + len(ISOC23))
        elif name not in RENAMELESS:
            remaining.append(f"{name.decode()}@{node.decode()}")
            continue

        struct.pack_into("<H", data, at, (raw & 0x8000) | versions.base)
        changed += 1

    return changed, remaining


def _abi_markers(nodes: dict[int, bytes]) -> list[str]:
    """Report each `GLIBC_ABI_*` node. A rename cannot reach one, and must not."""
    return [
        f"{node.decode()} -- {ABI_MARKERS.get(node, 'No correction is on record for this marker.')}"
        for node in nodes.values()
        if node.startswith(b"GLIBC_ABI_")
    ]


def _unreachable(nodes: dict[int, bytes], ceiling: tuple[int, ...], base_name: bytes) -> list[str]:
    """Report each node above the ceiling when the object omits the base node.

    A rename moves a symbol onto an entry that is already in `.gnu.version_r`.
    With no such entry there is nowhere to point, and this file adds none: a new
    entry resizes the section and moves every offset after it.
    """
    return [
        f"{node.decode()} -- the object does not reference {base_name.decode()}, so a rename has no entry to point at."
        for node in sorted(
            {name for name in nodes.values() if name.startswith(b"GLIBC_") and version_key(name) > ceiling}
        )
    ]


def _rename_unused_nodes(
    elf: Elf,
    dynsym: Section,
    versym: Section,
    versions: Versions,
    ceiling: tuple[int, ...],
) -> None:
    """Rename a node that lost every referent. One left in place keeps the floor."""
    data = elf.data
    used = {struct.unpack_from("<H", data, versym.offset + i * 2)[0] & 0x7FFF for i in range(dynsym.size // SYM_SIZE)}
    base_name_off = struct.unpack_from("<IHHII", data, versions.entry_at[versions.base])[3]

    for index, node in versions.nodes.items():
        if index in used or not node.startswith(b"GLIBC_") or version_key(node) <= ceiling:
            continue
        struct.pack_into("<I", data, versions.entry_at[index], elf_hash(versions.base_name))
        struct.pack_into("<I", data, versions.entry_at[index] + 8, base_name_off)


def lower(path: Path, ceiling: tuple[int, ...]) -> tuple[int, list[str]]:
    """Rewrite one object. Returns the count changed and what stays too new."""
    data = bytearray(path.read_bytes())
    if not is_elf64(data):
        return 0, []

    elf = Elf(data)
    dynsym = next((section for section in elf.sections if section.kind == SHT_DYNSYM), None)
    versym = elf.find(b".gnu.version")
    verneed = elf.find(b".gnu.version_r")
    if dynsym is None or versym is None or verneed is None:
        return 0, []

    nodes, entry_at = read_version_nodes(elf, verneed, elf.sections[dynsym.link].offset)
    # A marker has no symbol, so `_lower_symbols` never sees one. It is a failure
    # whether or not this object has anything to lower, so it is collected here
    # and it survives each early return below.
    markers = _abi_markers(nodes)

    base_name = BASE_NODES.get(elf.machine)
    if base_name is None:
        return 0, [*markers, f"e_machine {elf.machine} has no base version node on record."]

    # **An absent base node is a failure, and not an early return.** It was an
    # early return, and the aarch64 build then lowered nothing and reported
    # nothing: `GLIBC_2.2.5` was hard-coded, and no aarch64 object holds it.
    base = next((i for i, name in nodes.items() if name == base_name), None)
    if base is None:
        return 0, [*markers, *_unreachable(nodes, ceiling, base_name)]

    versions = Versions(nodes=nodes, entry_at=entry_at, base=base, base_name=base_name)

    changed, remaining = _lower_symbols(elf, dynsym, versym, versions, ceiling)
    remaining += markers
    if not changed:
        return 0, remaining

    _rename_unused_nodes(elf, dynsym, versym, versions, ceiling)
    path.write_bytes(bytes(data))
    return changed, remaining


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="2.34", help="the highest glibc node to allow")
    parser.add_argument("paths", nargs="+", type=Path)
    arguments = parser.parse_args()

    if not re.fullmatch(r"\d+\.\d+(\.\d+)?", arguments.target):
        sys.stderr.write(f"lower-glibc: '{arguments.target}' is not a glibc version\n")
        return 2
    ceiling = tuple(int(part) for part in arguments.target.split("."))

    failed = False
    for path in arguments.paths:
        if not path.is_file() or path.is_symlink():
            continue

        changed, remaining = lower(path, ceiling)
        if changed:
            sys.stdout.write(f"lower-glibc: {path.name}: {changed} symbols lowered to the base node\n")
        if remaining:
            failed = True
            listed = "".join(f"  {symbol}\n" for symbol in sorted(set(remaining)))
            sys.stderr.write(
                f"lower-glibc: {path} needs glibc above {arguments.target}, and a rename "
                f"cannot reach these:\n"
                f"{listed}"
                f"  A `name@GLIBC_x.y` entry is a function that glibc really added. Supply "
                f"it, or build the package without the feature that calls it.\n"
                f"  A `GLIBC_ABI_*` entry is a demand for an implementation, and the line "
                f"beside it names the compiler flag that removes it.\n"
                f"  The header of this file holds both, with the measurement behind each "
                f"one.\n"
            )

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
