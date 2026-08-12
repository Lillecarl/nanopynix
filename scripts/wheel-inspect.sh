#!/usr/bin/env bash
#
# Read a wheel and report the three numbers that decide whether it is
# publishable: the glibc floor, the count of objects that ask the host for a
# C++ standard library, and the payload size.
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
# - **Read `.dynsym`, and never `.symtab`.** `readelf -s` prints both, and only
#   the first one is what the loader reads. `nix/lower-glibc.py` rewrites
#   `.dynsym` alone, on purpose, so a lowered object keeps the original
#   `__isoc23_strtoul@GLIBC_2.38` in `.symtab` for as long as that table
#   survives the strip. Measured: `readelf -sW` reported 17 objects of the
#   x86-64 wheel above the floor, and `readelf --dyn-syms` reports none.
#   `auditwheel` agrees with the second one, and so does the loader.
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

printf '%-46s %-12s %-7s %9s\n' LIBRARY FLOOR HOSTC++ SIZE

worst=0
worst_name=""
hostcxx_total=0
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
    floor=$(readelf --dyn-syms --wide "$object" 2>/dev/null |
        awk '$7 == "UND" { print $8 }' |
        grep -oE 'GLIBC_[0-9]+\.[0-9]+' |
        sort -t. -k2,2n | tail -1 || true)
    [ -n "$floor" ] || floor="(none)"

    # **A C++ standard library of the host, in `DT_NEEDED`.** Counting
    # `__cxx11` symbols was the measure while the runtime was libc++, which
    # spells its own types `std::__1::`. The runtime is `libstdc++.a` now, so
    # every `__cxx11` symbol of the wheel is the private runtime answering for
    # itself: 3330 of them are in `libnanopynixcxx.so.1` alone, and the count
    # says nothing. What must stay zero is an object that asks the *host* for a
    # C++ runtime, which is gate 2 of `nix/wheel-gates.py`.
    hostcxx=$(readelf --dynamic --wide "$object" 2>/dev/null |
        grep -cE 'NEEDED.*(libstdc\+\+\.so|libc\+\+\.so|libc\+\+abi\.so)' || true)
    hostcxx_total=$((hostcxx_total + hostcxx))

    size=$(stat -Lc %s "$object")
    bytes=$((bytes + size))

    printf '%-46s %-12s %-7s %9s\n' \
        "$(basename "$object")" "$floor" "$hostcxx" "$(numfmt --to=iec "$size")"

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
echo "hostc++ : $hostcxx_total"
echo "payload : $(numfmt --to=iec "$bytes")"

if [ "$hostcxx_total" -ne 0 ]; then
    echo
    echo "FAIL: an object asks the host for a C++ standard library. Every C++ object" >&2
    echo "      of this wheel must take the private runtime of nix/cxx-runtime.nix," >&2
    echo "      so a package fell back to the stdenv of nixpkgs." >&2
    exit 1
fi
