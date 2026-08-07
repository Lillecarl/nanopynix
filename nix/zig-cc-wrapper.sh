#!@shell@
# A `zig cc` / `zig c++` that a nixpkgs build can use.
#
# It corrects four behaviours of zig 0.16. Each one is measured, and each one
# stops a real build.
#
# 0. Nothing writes an RPATH, so a library links and then cannot load.
#
#    nixpkgs adds one `-rpath` for each `-L` of the link, and it does that in
#    **ld-wrapper**. `zig cc` holds its own linker and never starts the `ld` of
#    the wrapper, so that step does not happen. A zig build of `libnixutil.so`
#    came out with an empty `RUNPATH` beside a reference build that names seven
#    directories, and a consumer of it then fails to find `libarchive.so.13`.
#
#    So this script does what ld-wrapper does: it mirrors each `-L` of the Nix
#    store into an `-rpath`. `fixupPhase` runs `patchelf --shrink-rpath` after
#    it, which removes each entry that the product does not need, so a wide
#    answer here is safe.
#
# 1. `--dependency-file` makes zig stop with SIGSEGV.
#
#    cmake gives that option to the linker on every shared library, to record
#    what the link read. zig answers with a segmentation fault, and the build
#    stops with `Error 139`. brotli is the first package to reach it. The file
#    only feeds the dependency tracking of cmake, and a derivation builds once,
#    so this script drops the option.
#
# 2. A compiler query fails whenever the target names a glibc version.
#
#    zig normalises `x86_64-linux-gnu.2.34` to `x86_64-unknown-linux-gnu.2.34`,
#    and then clang re-reads that text and answers `version '.2.34' in target
#    triple ... is invalid`. So `-dumpmachine` and `-print-search-dirs` exit 1,
#    and libtool stops at `checking dynamic linker characteristics`. A query
#    compiles nothing, so this script drops the version for a query only, and
#    the answer is then correct and the exit status is 0.
#
# 3. An executable names a loader that no NixOS machine has.
#
#    zig points every executable at `/lib64/ld-linux-x86-64.so.2`, and an
#    executable that names a loader which does not exist fails to start with
#    ENOENT. meson starts with a sanity check that compiles a program and runs
#    it, so a build stops there.
#
#    `-Wl,-dynamic-linker` does not correct this. zig gives the flag to its own
#    sub-compilations of compiler_rt, libubsan and the glibc shared objects, and
#    refuses it there with `LldCannotSpecifyDynamicLinkerForSharedLibraries`. So
#    this script patches the product instead, after the link.
#
#    A shared library carries no interpreter, so `--print-interpreter` fails on
#    one and the patch does not run. A wheel therefore takes nothing from this.
set -u

export ZIG_GLOBAL_CACHE_DIR="${ZIG_GLOBAL_CACHE_DIR:-${TMPDIR:-/tmp}/zig-cache}"

# A query asks the compiler about itself and writes no file.
is_query=0
# A step that stops before the link takes no `-rpath`.
is_link=1
# A link that names no libc takes no stack protection.
no_libc=0
for arg in "$@"; do
    case "$arg" in
    -dump* | -print-* | --print-*) is_query=1 ;;
    -c | -E | -S | -M | -MM) is_link=0 ;;
    -nostdlib | -nodefaultlibs) no_libc=1 ;;
    esac
done

# Rewrite the argument list once: drop `--dependency-file` in each of its
# spellings, and drop the glibc version from the target of a query. Each
# argument moves from the front of the list to the back, so the front always
# holds what this loop has not read yet.
count=$#
index=0
drop=0
previous=""
library_paths=""
while [ "$index" -lt "$count" ]; do
    arg=$1
    shift
    index=$((index + 1))

    if [ "$drop" -gt 0 ]; then
        drop=$((drop - 1))
        continue
    fi

    # Collect each `-L` of the Nix store, for the `-rpath` list below. A store
    # path holds no space, so one string separated by spaces is sufficient.
    case "$arg" in
    -L/nix/store/*) library_paths="$library_paths ${arg#-L}" ;;
    /nix/store/*)
        case "$previous" in
        -L) library_paths="$library_paths $arg" ;;
        esac
        ;;
    esac

    # zig refuses stack protection when the command names no libc, and gcc
    # accepts the pair and does nothing with it. libtool writes `-nostdlib` on
    # every C++ shared library that it links, and the stdenv of nixpkgs writes
    # `-fstack-protector-strong` on everything, so the two meet on any such
    # link: boehmgc stops with `unable to create module 'gc_badalc': enabling
    # stack protection requires libc`.
    #
    # A link produces no stack frame to protect, so dropping the flag there
    # costs nothing. Every compile keeps it.
    if [ "$no_libc" -eq 1 ]; then
        case "$arg" in
        -fstack-protector*) continue ;;
        esac
    fi

    case "$arg" in
    --dependency-file=* | -Wl,--dependency-file=*) continue ;;
    --dependency-file)
        drop=1
        continue
        ;;
    -Xlinker)
        case "${1-}" in
        --dependency-file=*)
            drop=1
            continue
            ;;
        --dependency-file)
            # `-Xlinker --dependency-file -Xlinker <path>`
            drop=3
            continue
            ;;
        esac
        ;;
    esac

    if [ "$is_query" -eq 1 ]; then
        case "$previous" in
        -target) arg=${arg%%.*} ;;
        esac
        case "$arg" in
        --target=*) arg=${arg%%.*} ;;
        esac
    fi

    previous=$arg
    set -- "$@" "$arg"
done

if [ "$is_query" -eq 1 ]; then
    exec "@zig@" @tool@ "$@"
fi

if [ "$is_link" -eq 1 ]; then
    for directory in $library_paths; do
        set -- "$@" "-Wl,-rpath,$directory"
    done
fi

"@zig@" @tool@ "$@"
status=$?
if [ "$status" -ne 0 ]; then
    exit "$status"
fi

# Find the output file. clang accepts both `-o name` and `-oname`, and it
# writes `a.out` when the caller gives neither.
output=""
take_next=0
for arg in "$@"; do
    if [ "$take_next" -eq 1 ]; then
        output=$arg
        take_next=0
        continue
    fi
    case "$arg" in
    -o) take_next=1 ;;
    -o*) output=${arg#-o} ;;
    esac
done
if [ -z "$output" ] && [ -f a.out ]; then
    output=a.out
fi
if [ -z "$output" ] || [ ! -f "$output" ]; then
    exit 0
fi

# `--print-interpreter` fails on a shared library and on an object file, so a
# failure here means there is nothing to correct.
interpreter=$("@patchelf@" --print-interpreter "$output" 2>/dev/null) || exit 0
case "$interpreter" in
/nix/store/*) exit 0 ;;
esac

"@patchelf@" --set-interpreter "@loader@" "$output"
"@patchelf@" --add-rpath "@libcLib@" "$output"
