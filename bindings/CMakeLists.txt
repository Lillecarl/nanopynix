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

# Nix 2.31 builds the complete builtin attribute set with a fixed capacity of
# 128 entries. It cannot safely accept arbitrary externally registered primops:
# overflowing that internal capacity aborts when EvalState is constructed.
if(NIX_EXPR_VERSION VERSION_LESS "2.32")
  set(NANOPYNIX_HAVE_DYNAMIC_PRIMOP_REGISTRATION 0)
else()
  set(NANOPYNIX_HAVE_DYNAMIC_PRIMOP_REGISTRATION 1)
endif()

set(CMAKE_REQUIRED_INCLUDES ${NIX_UTIL_INCLUDE_DIRS} ${NIX_STORE_INCLUDE_DIRS} ${NIX_EXPR_INCLUDE_DIRS})
set(CMAKE_REQUIRED_FLAGS "-std=c++23")
include(CheckCXXSourceCompiles)

check_cxx_source_compiles(
  "
  #include <memory>
  #include <type_traits>
  #include <nix/util/logging.hh>
  static_assert(std::is_same_v<decltype(nix::logger), std::unique_ptr<nix::Logger>>);
  int main() {}
  "
  NANOPYNIX_NIX_LOGGER_IS_UNIQUE_PTR
)
check_cxx_source_compiles(
  "
  #include <nix/store/build-result.hh>
  static_assert(requires (const nix::KeyedBuildResult & result) { result.tryGetSuccess(); });
  int main() {}
  "
  NANOPYNIX_HAVE_BUILD_RESULT_SUM
)
check_cxx_source_compiles(
  "
  #include <nix/expr/eval.hh>
  static_assert(requires (nix::EvalState & state) { state.mem; });
  int main() {}
  "
  NANOPYNIX_HAVE_EVALSTATE_MEM
)
check_cxx_source_compiles(
  "
  #include <nix/expr/eval.hh>
  static_assert(requires (nix::PrimOp op) { op.impl; });
  int main() {}
  "
  NANOPYNIX_PRIMOP_USES_IMPL
)
check_cxx_source_compiles(
  "
  #include <optional>
  #include <string>
  #include <type_traits>
  #include <nix/expr/eval.hh>
  static_assert(std::is_same_v<decltype(nix::PrimOp::doc), std::optional<std::string>>);
  int main() {}
  "
  NANOPYNIX_PRIMOP_DOC_IS_OPTIONAL
)
check_cxx_source_compiles(
  "
  #include <memory>
  #include <nix/store/realisation.hh>
  std::shared_ptr<const nix::UnkeyedRealisation> value;
  int main() {}
  "
  NANOPYNIX_HAVE_UNKEYED_REALISATION
)
check_cxx_source_compiles(
  "
  #include <memory>
  #include <nix/store/store-api.hh>
  static_assert(requires (nix::Store & store, const nix::StorePath & path) {
    store.getFSAccessor(path, true);
  });
  int main() {}
  "
  NANOPYNIX_HAVE_PATH_FS_ACCESSOR
)
check_cxx_source_compiles(
  "
  #include <nix/store/path-info.hh>
  #include <nix/store/store-api.hh>
  #include <nix/util/hash.hh>
  static_assert(requires (const nix::StoreDirConfig & config, nix::Hash hash) {
    nix::UnkeyedValidPathInfo(config, hash);
  });
  int main() {}
  "
  NANOPYNIX_PATH_INFO_NEEDS_STORE_CONFIG
)
check_cxx_source_compiles(
  "
  #include <nix/fetchers/fetchers.hh>
  #include <nix/store/store-api.hh>
  static_assert(requires (const nix::fetchers::Input & input, nix::Store & store) {
    input.getFingerprint(store);
  });
  int main() {}
  "
  NANOPYNIX_FETCHER_FINGERPRINT_TAKES_STORE_REF
)
check_cxx_source_compiles(
  "
  #include <nix/util/url.hh>
  static_assert(requires { nix::fixGitURL(\"git@example.org:repo\").to_string(); });
  int main() {}
  "
  NANOPYNIX_FIX_GIT_URL_RETURNS_URL
)
check_cxx_source_compiles(
  "
  #include <string>
  #include <type_traits>
  #include <nix/util/types.hh>
  static_assert(std::is_same_v<nix::Path, std::string>);
  int main() {}
  "
  NANOPYNIX_PATH_IS_STRING
)
check_cxx_source_compiles(
  "
  #include <nix/store/gc-store.hh>
  nix::GCAction value;
  int main() {}
  "
  NANOPYNIX_HAVE_GLOBAL_GC_ACTION
)
check_cxx_source_compiles(
  "
  #include <nix/store/gc-store.hh>
  static_assert(requires { typename nix::GCOptions::SpecificPaths; });
  int main() {}
  "
  NANOPYNIX_GC_PATHS_IS_VARIANT
)
check_cxx_source_compiles(
  "
  #include <nix/store/store-api.hh>
  class StoreAnchorProbe : public nix::Store {
    void anchor() override {}
  };
  int main() {}
  "
  NANOPYNIX_HAVE_STORE_ANCHOR
)
check_cxx_source_compiles(
  "
  #include <nix/store/store-api.hh>
  class StoreConfigProbe : public nix::StoreConfig {
  public:
    StoreConfigProbe(const Params & params)
      : StoreConfig(params, FilePathType::Unix) {}
  };
  int main() {}
  "
  NANOPYNIX_STORE_CONFIG_NEEDS_PATH_TYPE
)
check_cxx_source_compiles(
  "
  #include <nix/util/source-path.hh>
  static_assert(requires (const nix::SourcePath & path) { path.invalidateCache(); });
  int main() {}
  "
  NANOPYNIX_HAVE_SOURCE_PATH_INVALIDATE_CACHE
)
check_cxx_source_compiles(
  "
  #include <filesystem>
  #include <nix/util/source-accessor.hh>
  static_assert(requires (const std::filesystem::path & path) { nix::makeFSSourceAccessor(path); });
  int main() {}
  "
  NANOPYNIX_HAVE_MAKE_FS_SOURCE_ACCESSOR
)
check_cxx_source_compiles(
  "
  #include <string_view>
  #include <nix/flake/lockfile.hh>
  static_assert(requires { nix::flake::NonEmptyInputAttrPath::parse(std::string_view{}); });
  int main() {}
  "
  NANOPYNIX_HAVE_NONEMPTY_INPUT_ATTR_PATH
)

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
  nanobind_add_module(nanopynix_util src/nix_util.cpp)
  target_include_directories(nanopynix_util PRIVATE ${NIX_UTIL_INCLUDE_DIRS} ${NIX_STORE_INCLUDE_DIRS})
  target_link_libraries(nanopynix_util PRIVATE ${NIX_UTIL_LIBRARIES} ${NIX_STORE_LIBRARIES})
  target_compile_options(nanopynix_util PRIVATE -std=c++23)
  nanopynix_target_uses_nix_compat(nanopynix_util)
endif()

# --- nanopynix_store ---
if(BUILD_STORE)
  nanobind_add_module(nanopynix_store src/nix_store.cpp src/py_store_impl.cpp)
  target_include_directories(nanopynix_store PRIVATE ${NIX_STORE_INCLUDE_DIRS} ${NIX_UTIL_INCLUDE_DIRS})
  target_link_libraries(nanopynix_store PRIVATE ${NIX_STORE_LIBRARIES} ${NIX_UTIL_LIBRARIES})
  target_compile_options(nanopynix_store PRIVATE -std=c++23)
  nanopynix_target_uses_nix_compat(nanopynix_store)
endif()

# --- nanopynix_expr ---
if(BUILD_EXPR)
  nanobind_add_module(nanopynix_expr src/nix_expr.cpp)
  target_include_directories(nanopynix_expr PRIVATE
    ${NIX_EXPR_INCLUDE_DIRS} ${NIX_STORE_INCLUDE_DIRS} ${NIX_UTIL_INCLUDE_DIRS})
  target_link_libraries(nanopynix_expr PRIVATE
    ${NIX_EXPR_LIBRARIES} ${NIX_STORE_LIBRARIES} ${NIX_UTIL_LIBRARIES})
  target_compile_options(nanopynix_expr PRIVATE -std=c++23)
  nanopynix_target_uses_nix_compat(nanopynix_expr)
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
  nanobind_add_module(nanopynix_fetchers src/nix_fetchers.cpp)
  target_include_directories(nanopynix_fetchers PRIVATE
    ${NIX_FETCHERS_INCLUDE_DIRS} ${NIX_STORE_INCLUDE_DIRS} ${NIX_UTIL_INCLUDE_DIRS})
  target_link_libraries(nanopynix_fetchers PRIVATE
    ${NIX_FETCHERS_LIBRARIES} ${NIX_STORE_LIBRARIES} ${NIX_UTIL_LIBRARIES})
  target_compile_options(nanopynix_fetchers PRIVATE -std=c++23)
  nanopynix_target_uses_nix_compat(nanopynix_fetchers)
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
  nanobind_add_module(nanopynix_flake src/nix_flake.cpp)
  target_include_directories(nanopynix_flake PRIVATE
    ${NIX_FLAKE_INCLUDE_DIRS} ${NIX_EXPR_INCLUDE_DIRS}
    ${NIX_FETCHERS_INCLUDE_DIRS} ${NIX_STORE_INCLUDE_DIRS} ${NIX_UTIL_INCLUDE_DIRS})
  target_link_libraries(nanopynix_flake PRIVATE
    ${NIX_FLAKE_LIBRARIES} ${NIX_EXPR_LIBRARIES}
    ${NIX_FETCHERS_LIBRARIES} ${NIX_STORE_LIBRARIES} ${NIX_UTIL_LIBRARIES})
  target_compile_options(nanopynix_flake PRIVATE -std=c++23)
  nanopynix_target_uses_nix_compat(nanopynix_flake)
endif()

# --- nanopynix_main ---
if(BUILD_MAIN)
  pkg_check_modules(NIX_MAIN REQUIRED nix-main)
  nanobind_add_module(nanopynix_main src/nix_main.cpp)
  target_include_directories(nanopynix_main PRIVATE ${NIX_MAIN_INCLUDE_DIRS})
  target_link_libraries(nanopynix_main PRIVATE ${NIX_MAIN_LIBRARIES})
  target_compile_options(nanopynix_main PRIVATE -std=c++23)
  nanopynix_target_uses_nix_compat(nanopynix_main)
endif()

# --- Install rules ---
if(BUILD_UTIL)
  install(TARGETS nanopynix_util LIBRARY DESTINATION .)
endif()
if(BUILD_STORE)
  install(TARGETS nanopynix_store LIBRARY DESTINATION .)
endif()
if(BUILD_EXPR)
  install(TARGETS nanopynix_expr LIBRARY DESTINATION .)
endif()
if(BUILD_FETCHERS)
  install(TARGETS nanopynix_fetchers LIBRARY DESTINATION .)
endif()
if(BUILD_FLAKE)
  install(TARGETS nanopynix_flake LIBRARY DESTINATION .)
endif()
if(BUILD_MAIN)
  install(TARGETS nanopynix_main LIBRARY DESTINATION .)
endif()
