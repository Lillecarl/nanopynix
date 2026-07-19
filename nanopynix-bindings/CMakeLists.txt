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
pkg_check_modules(NIX_CMD REQUIRED nix-cmd)
# nix-flake, so builtins.getFlake is registered on every EvalState this
# module constructs (see nix_expr.cpp's evalSettingsConfigurators call) --
# pkg-config resolves nix-flake's own transitive Requires (nix-fetchers, ...)
# automatically, so NIX_FLAKE_LIBRARIES/_INCLUDE_DIRS already cover them.
pkg_check_modules(NIX_FLAKE REQUIRED nix-flake)

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

option(BUILD_UTIL     "Build nanopynix_util"     ON)
option(BUILD_STORE    "Build nanopynix_store"    ON)
option(BUILD_EXPR     "Build nanopynix_expr"     ON)
option(BUILD_FETCHERS "Build nanopynix_fetchers" ON)
option(BUILD_FLAKE    "Build nanopynix_flake"    ON)
option(BUILD_MAIN     "Build nanopynix_main"     ON)

# --- nanopynix_util ---
if(BUILD_UTIL)
  nanobind_add_module(util src/nix_util.cpp)
  target_include_directories(util PRIVATE ${NIX_UTIL_INCLUDE_DIRS} ${NIX_STORE_INCLUDE_DIRS})
  target_link_libraries(util PRIVATE ${NIX_UTIL_LIBRARIES} ${NIX_STORE_LIBRARIES})
  target_compile_options(util PRIVATE -std=c++23)
  nanopynix_target_uses_nix_compat(util)
endif()

# --- nanopynix_store ---
if(BUILD_STORE)
  nanobind_add_module(store src/nix_store.cpp src/py_store_impl.cpp)
  target_include_directories(store PRIVATE ${NIX_STORE_INCLUDE_DIRS} ${NIX_UTIL_INCLUDE_DIRS})
  target_link_libraries(store PRIVATE ${NIX_STORE_LIBRARIES} ${NIX_UTIL_LIBRARIES})
  target_compile_options(store PRIVATE -std=c++23)
  nanopynix_target_uses_nix_compat(store)
endif()

# --- nanopynix_expr ---
if(BUILD_EXPR)
  nanobind_add_module(expr src/nix_expr.cpp)
  target_include_directories(expr PRIVATE
    ${NIX_CMD_INCLUDE_DIRS} ${NIX_EXPR_INCLUDE_DIRS} ${NIX_STORE_INCLUDE_DIRS} ${NIX_UTIL_INCLUDE_DIRS}
    ${NIX_FLAKE_INCLUDE_DIRS})
  target_link_libraries(expr PRIVATE
    ${NIX_CMD_LIBRARIES} ${NIX_EXPR_LIBRARIES} ${NIX_STORE_LIBRARIES} ${NIX_UTIL_LIBRARIES}
    ${NIX_FLAKE_LIBRARIES})
  # nix-expr propagates bdw-gc. Its public thread-registration declarations
  # are intentionally visible only to consumers defining GC_THREADS.
  target_compile_definitions(expr PRIVATE GC_THREADS)
  target_compile_options(expr PRIVATE -std=c++23)
  nanopynix_target_uses_nix_compat(expr)
endif()

# --- nanopynix_fetchers ---
if(BUILD_FETCHERS)
  if(NOT DEFINED NIX_STORE_LIBRARIES)
    pkg_check_modules(NIX_STORE    REQUIRED nix-store)
  endif()
  if(NOT DEFINED NIX_UTIL_LIBRARIES)
    pkg_check_modules(NIX_UTIL     REQUIRED nix-util)
  endif()
  pkg_check_modules(NIX_FETCHERS   REQUIRED nix-fetchers)
  nanobind_add_module(fetchers src/nix_fetchers.cpp)
  target_include_directories(fetchers PRIVATE
    ${NIX_FETCHERS_INCLUDE_DIRS} ${NIX_STORE_INCLUDE_DIRS} ${NIX_UTIL_INCLUDE_DIRS})
  target_link_libraries(fetchers PRIVATE
    ${NIX_FETCHERS_LIBRARIES} ${NIX_STORE_LIBRARIES} ${NIX_UTIL_LIBRARIES})
  target_compile_options(fetchers PRIVATE -std=c++23)
  nanopynix_target_uses_nix_compat(fetchers)
endif()

# --- nanopynix_flake ---
if(BUILD_FLAKE)
  if(NOT DEFINED NIX_STORE_LIBRARIES)
    pkg_check_modules(NIX_STORE    REQUIRED nix-store)
  endif()
  if(NOT DEFINED NIX_UTIL_LIBRARIES)
    pkg_check_modules(NIX_UTIL     REQUIRED nix-util)
  endif()
  pkg_check_modules(NIX_EXPR       REQUIRED nix-expr)
  pkg_check_modules(NIX_FETCHERS   REQUIRED nix-fetchers)
  pkg_check_modules(NIX_FLAKE      REQUIRED nix-flake)
  nanobind_add_module(flake src/nix_flake.cpp)
  target_include_directories(flake PRIVATE
    ${NIX_FLAKE_INCLUDE_DIRS} ${NIX_EXPR_INCLUDE_DIRS}
    ${NIX_FETCHERS_INCLUDE_DIRS} ${NIX_STORE_INCLUDE_DIRS} ${NIX_UTIL_INCLUDE_DIRS})
  target_link_libraries(flake PRIVATE
    ${NIX_FLAKE_LIBRARIES} ${NIX_EXPR_LIBRARIES}
    ${NIX_FETCHERS_LIBRARIES} ${NIX_STORE_LIBRARIES} ${NIX_UTIL_LIBRARIES})
  target_compile_options(flake PRIVATE -std=c++23)
  nanopynix_target_uses_nix_compat(flake)
endif()

# --- nanopynix_main ---
if(BUILD_MAIN)
  pkg_check_modules(NIX_MAIN REQUIRED nix-main)
  nanobind_add_module(main src/nix_main.cpp)
  target_include_directories(main PRIVATE ${NIX_MAIN_INCLUDE_DIRS})
  target_link_libraries(main PRIVATE ${NIX_MAIN_LIBRARIES})
  target_compile_options(main PRIVATE -std=c++23)
  nanopynix_target_uses_nix_compat(main)
endif()

# --- Install rules ---
if(BUILD_UTIL)
  install(TARGETS util LIBRARY DESTINATION nanopynix_bindings)
endif()
if(BUILD_STORE)
  install(TARGETS store LIBRARY DESTINATION nanopynix_bindings)
endif()
if(BUILD_EXPR)
  install(TARGETS expr LIBRARY DESTINATION nanopynix_bindings)
endif()
if(BUILD_FETCHERS)
  install(TARGETS fetchers LIBRARY DESTINATION nanopynix_bindings)
endif()
if(BUILD_FLAKE)
  install(TARGETS flake LIBRARY DESTINATION nanopynix_bindings)
endif()
if(BUILD_MAIN)
  install(TARGETS main LIBRARY DESTINATION nanopynix_bindings)
endif()

install(FILES src/nanopynix_bindings/__init__.py DESTINATION nanopynix_bindings)
