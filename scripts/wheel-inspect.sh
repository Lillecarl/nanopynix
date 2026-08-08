#!/usr/bin/env bash
#
# Read a wheel and report the three numbers that decide whether it is
# publishable: the glibc floor, the count of libstdc++ ABI symbols, and the
# payload size.
#
# **This reads files, and it runs nothing.** So it answers for a wheel of any
# architecture, which `scripts/wheel-smoke.sh` cannot: that script needs an
# interpreter of the wheel's architecture. Use both. This one says the wheel is
# built right, and the other says the wheel works.
#
# Two rules that a hand-written check gets wrong:
#
# - **The floor comes from the UNDEFINED symbols only.** A library also
#   *defines* versioned symbols of its own, and counting those reports the
#   glibc that built it rather than the glibc it needs.
# - **The payload is the wheel, and not the Nix store closure.** A closure walk
#   includes CPython, its stdlib extension modules, glibc and libstdc++. Such a
#   walk reported `GLIBC_2.42` and 3269 `__cxx11` symbols for a wheel that
#   holds neither.
#
# Usage:
#   nix build --file . nanopynixWheel --out-link result-wheel
#   scripts/wheel-inspect.sh result-wheel

set -euo pipefail

wheel_dir=${1:?usage: wheel-inspect.sh <directory holding the wheel>}
wheel_dir=$(readlink -f "$wheel_dir")

wheel=$(find "$wheel_dir" -maxdepth 1 -name '*.whl' | head -1)
if [ -z "$wheel" ]; then
    echo "wheel-inspect: no wheel in $wheel_dir" >&2
    exit 1
fi

work_dir=$(mktemp -d -t nanopynix-wheel-inspect-XXXXXX)
trap 'rm -rf "$work_dir"' EXIT

python3 -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' \
    "$wheel" "$work_dir"

echo "wheel   : $(basename "$wheel")"
echo "size    : $(du -h "$wheel" | cut -f1)"
echo

printf '%-46s %-12s %-7s %9s\n' LIBRARY FLOOR CXX11 SIZE

worst=0
worst_name=""
cxx11_total=0
bytes=0
count=0

while IFS= read -r object; do
    count=$((count + 1))

    # `|| true`, because a library can have no undefined glibc symbol at all
    # and `grep` then exits 1. `libaws-checksums.so` and
    # `libboost_date_time.so` are both like that, and it is correct rather than
    # a gap. With `pipefail` and `set -e` the missing `|| true` ended the
    # script in the middle of the table, and the exit status was hidden because
    # the caller piped the output into `tail`.
    floor=$(readelf -sW --dyn-syms "$object" 2>/dev/null |
        awk '$7 == "UND" { print $8 }' |
        grep -oE 'GLIBC_[0-9]+\.[0-9]+' |
        sort -t. -k2,2n | tail -1 || true)
    [ -n "$floor" ] || floor="(none)"

    cxx11=$(readelf -sW --dyn-syms "$object" 2>/dev/null | grep -c '__cxx11' || true)
    cxx11_total=$((cxx11_total + cxx11))

    size=$(stat -Lc %s "$object")
    bytes=$((bytes + size))

    printf '%-46s %-12s %-7s %9s\n' \
        "$(basename "$object")" "$floor" "$cxx11" "$(numfmt --to=iec "$size")"

    # A glibc symbol version is `GLIBC_2.34`, and it is also `GLIBC_2.2.5` and
    # `GLIBC_2.3.4`. Take the second field alone: a three-part version is older
    # than every two-part one that matters here, and `[ 2.5 -gt 34 ]` is not an
    # integer comparison, so `set -e` ends the script in the middle of the
    # table.
    minor=${floor#GLIBC_2.}
    minor=${minor%%.*}
    if [ "$floor" != "(none)" ] && [ "$minor" -gt "$worst" ] 2>/dev/null; then
        worst=$minor
        worst_name=$(basename "$object")
    fi
done < <(find "$work_dir" \( -name '*.so' -o -name '*.so.*' \) -type f | sort)

echo
echo "objects : $count"
echo "arch    : $(readelf -hW "$(find "$work_dir" -name '_ext*.so' | head -1)" | awk '/Machine:/ { $1=""; print substr($0,2) }')"
echo "floor   : GLIBC_2.$worst   (set by $worst_name)"
echo "__cxx11 : $cxx11_total"
echo "payload : $(numfmt --to=iec "$bytes")"

if [ "$cxx11_total" -ne 0 ]; then
    echo
    echo "FAIL: the wheel carries the libstdc++ ABI. The zig closure is meant to" >&2
    echo "      remove it, so a package fell back to the stdenv of nixpkgs." >&2
    exit 1
fi
