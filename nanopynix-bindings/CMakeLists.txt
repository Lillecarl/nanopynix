cmake_minimum_required(VERSION 3.15...3.27)
project(nanopynix)

if (NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Choose the type of build." FORCE)
  set_property(CACHE CMAKE_BUILD_TYPE PROPERTY STRINGS "Debug" "Release" "MinSizeRel" "RelWithDebInfo")
endif()

if (CMAKE_VERSION VERSION_LESS 3.18)
  set(DEV_MODULE Development)
else()
  set(DEV_MODULE Development.Module)
endif()
find_package(Python 3.9 COMPONENTS Interpreter ${DEV_MODULE} REQUIRED)
find_package(nanobind CONFIG REQUIRED)
find_package(PkgConfig REQUIRED)

pkg_check_modules(NIX_UTIL REQUIRED nix-util)
pkg_check_modules(NIX_STORE REQUIRED nix-store)
pkg_check_modules(NIX_EXPR REQUIRED nix-expr)
pkg_check_modules(NIX_FETCHERS REQUIRED nix-fetchers)
# nix-flake, so builtins.getFlake is registered on every EvalState (see
# nix_flake.cpp's evalSettingsConfigurators call).
pkg_check_modules(NIX_FLAKE REQUIRED nix-flake)

# Deliberately absent: nix-cmd. The only symbol nanopynix ever took from it was
# `nix::lookupFileArg`, and src/nix_expr.cpp now implements that itself against
# the EvalState's own fetch settings instead of libcmd's process-global ones --
# see the comment on `lookup_file_arg` there for why that is a fix and not just
# a dependency trim. Linking libcmd also dlopened lowdown and editline into
# every Python process that imports nanopynix: a Markdown renderer and a line
# editor, neither of which a library binding has any use for.

# Nix's C++ bindings are a release stream, not an independent feature matrix.
# Compatibility uses ordered release bands: < 2.32, < 2.35, then the latest
# default branch. New releases therefore select the final source branch unless
# an API is deliberately assigned another cutoff.
string(REGEX MATCH "^([0-9]+)\\.([0-9]+)" _nix_version_match "${NIX_STORE_VERSION}")
if(NOT _nix_version_match)
  message(FATAL_ERROR "Cannot parse nix-store version: ${NIX_STORE_VERSION}")
endif()
set(NANOPYNIX_NIX_VERSION_MAJOR "${CMAKE_MATCH_1}")
set(NANOPYNIX_NIX_VERSION_MINOR "${CMAKE_MATCH_2}")
math(EXPR NANOPYNIX_NIX_VERSION_NUMBER
  "${NANOPYNIX_NIX_VERSION_MAJOR} * 1000 + ${NANOPYNIX_NIX_VERSION_MINOR}")

file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/nanopynix")
configure_file(
  src/nix_compat_config.hh
  "${CMAKE_CURRENT_BINARY_DIR}/nanopynix/nix_compat_config.hh"
  @ONLY
)

function(nanopynix_target_uses_nix_compat target)
  target_include_directories(${target} PRIVATE "${CMAKE_CURRENT_BINARY_DIR}")
endfunction()

# Record only the libraries a module actually resolves a symbol from.
#
# pkg-config expands each component's transitive `Requires` into
# NIX_*_LIBRARIES, so a target naming one Nix component ends up on a link line
# naming five, and every one of them became a DT_NEEDED entry whether or not a
# single symbol came from it. Measured on the 2.35 build before this was added,
# five of the twenty-four edges were dead: the old main.so declared libnixexpr,
# libnixfetchers, libnixstore and libnixutil and used nothing from any of them;
# errors.so declared libnixfetchers, flake.so libnixstore, likewise.
#
# Those edges cost load-time work in every process that imports the package and
# they misdescribe the dependency graph to anyone reading it. --as-needed drops
# them at link time, and does it per Nix version, which hand-pruning the
# pkg_check_modules lists could not: which component owns a given symbol has
# moved between 2.31, 2.34 and 2.35 more than once.
#
# Safe here because nothing nanopynix links is loaded purely for its static
# initialisers -- every Nix component it names, it calls into. The failure mode
# if that ever stops being true is a missing registration at runtime, so a new
# component pulled in for its side effects alone needs `-Wl,--no-as-needed`
# around it and a comment saying why.
function(nanopynix_link_as_needed target)
  target_link_options(${target} PRIVATE "LINKER:--as-needed")
endfunction()

# --- the extension ---
#
# One target, one translation unit per area. The BUILD_* options that used to
# gate each module individually are gone with the separate targets: they were
# pinned ON in pyproject.toml and nothing else ever set them, and a partial
# build is not meaningful now that the areas share a single module init.
#
# The union of the include dirs and libraries the areas asked for separately.
# Deliberately absent alongside nix-cmd: nix-main. It existed only to bind
# `initNix`/`initPlugins`, which nanopynix removed -- see the `main` area's
# removal for why a library has no business installing signal handlers or
# dlopening ABI-coupled C++ plugins named by someone's nix.conf. --as-needed prunes whatever this build does not actually resolve
# a symbol from, so the union costs nothing but says plainly which Nix
# components the bindings are built against.
nanobind_add_module(_ext
  src/nanopynix_module.cpp
  src/nix_errors.cpp
  src/nix_signals.cpp
  src/nix_util.cpp
  src/nix_store.cpp
  src/py_store_impl.cpp
  src/nix_expr.cpp
  src/nix_fetchers.cpp
  src/nix_flake.cpp
)
target_include_directories(_ext PRIVATE
  ${NIX_FLAKE_INCLUDE_DIRS} ${NIX_FETCHERS_INCLUDE_DIRS}
  ${NIX_EXPR_INCLUDE_DIRS} ${NIX_STORE_INCLUDE_DIRS} ${NIX_UTIL_INCLUDE_DIRS})
target_link_libraries(_ext PRIVATE
  ${NIX_FLAKE_LIBRARIES} ${NIX_FETCHERS_LIBRARIES}
  ${NIX_EXPR_LIBRARIES} ${NIX_STORE_LIBRARIES} ${NIX_UTIL_LIBRARIES})
# nix-expr propagates bdw-gc. Its public thread-registration declarations are
# intentionally visible only to consumers defining GC_THREADS. Only
# src/nix_expr.cpp includes <gc/gc.h>, but the define is per-target.
#
# Unconditional, and safe against a `-Dgc=disabled` libexpr: that build
# installs no Boehm headers, and src/nix_expr.cpp guards its include with
# NIX_USE_BOEHMGC, so nothing here reads the define. A define that reaches no
# header costs nothing, and one CMake branch fewer is one place fewer for the
# two builds to drift.
target_compile_definitions(_ext PRIVATE GC_THREADS)
target_compile_options(_ext PRIVATE -std=c++23)
nanopynix_target_uses_nix_compat(_ext)
nanopynix_link_as_needed(_ext)

# What this extension exports. `src/export.map` gives the whole reason; the
# short form is that `-fvisibility-ms-compat` (set by the Nix build) has to
# export the type information so a `catch` and a `dynamic_cast` work across the
# boundary to `libnixexpr.so`, and it exports 23 boost functions with it.
# CPython opens this extension as the root of a `dlopen` scope, so those
# functions shadow the copies inside `libnixexpr.so`, and `builtins.throw` then
# ends in `double free or corruption`.
#
# Here, and not in `cmakeFlags` of `package.nix`: each entry there becomes its
# own `-Ccmake.args=` token and must hold no space, so it cannot carry both
# this and the linker flag of a sanitizer build.
target_link_options(_ext PRIVATE
  "LINKER:--version-script=${CMAKE_CURRENT_SOURCE_DIR}/src/export.map")

# --- Install rules ---
install(TARGETS _ext LIBRARY DESTINATION nanopynix_bindings)
# The whole directory, not a file list: it holds `__init__.py` plus one
# republishing module per area, and a list here would be a second place to
# remember when an area is added.
#
# `*.pyi` is named explicitly because CMake's PATTERN is a suffix match rather
# than a full-name glob, so `*.py` already drags `_ext.pyi` along -- better to
# say so than to leave it looking accidental. Shipping it is right: the
# installed tree has the real extension, and the stub gives a type checker the
# submodule names the republishing modules read. The per-area `.pyi` files are
# not here; postInstall generates those from the built module.
install(DIRECTORY src/nanopynix_bindings/ DESTINATION nanopynix_bindings
        FILES_MATCHING PATTERN "*.py" PATTERN "*.pyi")
