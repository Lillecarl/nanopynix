"""Put the licence of every bundled library into an unpacked wheel.

`nix/wheel.nix` runs this between `auditwheel repair` and `wheel pack`.
`auditwheel` copies 44 libraries that this project did not write into
`nanopynix_bindings.libs/`, and every one of their licences obliges the
redistributor to carry the licence text and the copyright notice. This module
writes both, and it fails the build when it cannot.

**It reads the objects that are in the wheel.** The build closure is larger:
`nix/zig-nix.nix` also builds zlib, attr, lzo and libev, and `auditwheel`
bundles none of the four. A notice taken from the closure would name a GPL
library that the wheel does not carry.

Two conditions end the build, and both mean that the wheel changed and the
licence data did not:

1. A bundled object resolves to no package. `nix/wheel-licenses.nix` maps every
   soname of the whole closure, so this means a library arrived from outside
   that closure.
2. A bundled object resolves to a package that has no licence entry. The
   message names the package, and the correction is an entry in
   `nix/wheel-licenses.nix`.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# `auditwheel` renames each library it copies, from `libcurl.so.4.8.0` to
# `libcurl-a1b2c3d4.so.4.8.0`. The hash keeps two wheels in one process from
# sharing a library, and it is 8 hexadecimal characters before the `.so`.
AUDITWHEEL_HASH = re.compile(r"-[0-9a-f]{8}(?=\.so)")

HEADER = """\
# Third-party licences

`nanopynix-bindings` is a wheel with its libraries inside it. This file names
every library in `nanopynix_bindings.libs/`, gives its licence, and points at
the full text.

The text of each licence is in `third-party/<name>/` beside this file. The
licence of `nanopynix-bindings` itself is in `LICENSE`.

**Two of these libraries are under the LGPL**, and one is under the GPL with a
linking exception. None of the three imposes a condition on a program that
imports `nanopynix_bindings`. Each rides in the wheel as a separate shared
object, which is what the LGPL asks for: replace the file and the wheel uses
the replacement. The source of every library is a fixed revision of nixpkgs,
and `flake.lock` in the repository names it.
"""


ARGUMENT_COUNT = 4


def resolve(
    libs: list[Path],
    sonames: dict[str, str],
    manifest: dict[str, dict[str, str | None]],
) -> tuple[dict[str, list[str]], list[str], dict[str, list[str]]]:
    """Group the bundled objects by package, and separate the two failures."""
    bundled: dict[str, list[str]] = {}
    unresolved: list[str] = []
    unlicensed: dict[str, list[str]] = {}

    for lib in libs:
        soname = AUDITWHEEL_HASH.sub("", lib.name)
        package = sonames.get(soname)
        if package is None:
            unresolved.append(lib.name)
        elif package not in manifest:
            unlicensed.setdefault(package, []).append(lib.name)
        else:
            bundled.setdefault(package, []).append(lib.name)

    return bundled, unresolved, unlicensed


def write_notices(
    target: Path,
    licences: Path,
    manifest: dict[str, dict[str, str | None]],
    bundled: dict[str, list[str]],
    object_count: int,
) -> int:
    """Copy each licence text into the wheel, and write the summary. Give the
    number of packages whose licence differs from the packaging metadata."""
    third_party = target / "third-party"
    third_party.mkdir(parents=True, exist_ok=True)

    rows: list[str] = []
    notes: list[str] = []

    for package in sorted(bundled):
        entry = manifest[package]
        destination = third_party / package
        destination.mkdir(exist_ok=True)
        for text in sorted((licences / "texts" / package).iterdir()):
            destination.joinpath(text.name).write_bytes(text.read_bytes())

        objects = ", ".join(f"`{name}`" for name in sorted(bundled[package]))
        homepage = entry["homepage"] or ""
        rows.append(f"| {package} | {entry['version']} | {entry['spdx']} | {objects} | {homepage} |")
        note = entry["note"]
        if note:
            notes.append(f"### {package}\n\n{note.strip()}\n")

    document = [
        HEADER,
        f"\n## The {object_count} libraries\n",
        "| package | version | licence | object | upstream |",
        "| --- | --- | --- | --- | --- |",
        *rows,
    ]
    if notes:
        document += [
            "\n## Where the licence differs from the packaging metadata\n",
            "nixpkgs records a licence for a whole project, and this wheel carries one",
            "library out of it. Each entry below gives the file that was read.\n",
            *notes,
        ]

    (target / "THIRD-PARTY-NOTICES.md").write_text("\n".join(document) + "\n")
    return len(notes)


def main() -> int:
    if len(sys.argv) != ARGUMENT_COUNT:
        sys.stderr.write("usage: wheel-notice.py <unpacked wheel> <licences> <dist-info>\n")
        return 2

    unpacked = Path(sys.argv[1])
    licences = Path(sys.argv[2])
    dist_info = unpacked / sys.argv[3]

    sonames: dict[str, str] = {}
    for line in (licences / "sonames.tsv").read_text().splitlines():
        soname, package = line.split("\t")
        sonames[soname] = package

    manifest = json.loads((licences / "packages.json").read_text())

    libs = sorted(unpacked.glob("*.libs/*"))
    if not libs:
        sys.stderr.write(
            "wheel-notice: the wheel holds no `*.libs/` directory.\n"
            "  `auditwheel repair` did not run, or it bundled nothing.\n"
        )
        return 1

    bundled, unresolved, unlicensed = resolve(libs, sonames, manifest)

    if unresolved:
        sys.stderr.write(
            "wheel-notice: these objects belong to no package of the closure:\n"
            + "".join(f"  {name}\n" for name in sorted(unresolved))
            + "  A library reached the wheel from outside `nix/zig-nix.nix`, so its\n"
            "  licence is unknown. Add the package to that file.\n"
        )
        return 1

    if unlicensed:
        sys.stderr.write(
            "wheel-notice: these packages are in the wheel and have no licence entry:\n"
            + "".join(f"  {package}: {', '.join(sorted(names))}\n" for package, names in sorted(unlicensed.items()))
            + "  The wheel redistributes each one, so it has to carry the licence text.\n"
            "  Read the source, and add an entry to `files` in nix/wheel-licenses.nix.\n"
        )
        return 1

    corrections = write_notices(dist_info / "licenses", licences, manifest, bundled, len(libs))
    sys.stdout.write(f"wheel-notice: {len(libs)} objects, {len(bundled)} packages, {corrections} corrections\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
