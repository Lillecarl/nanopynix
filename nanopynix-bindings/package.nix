{
  lib,
  buildPythonPackage,
  pkg-config,
  python,
  nanobind,
  nix-util,
  nix-store,
  nix-expr,
  nix-fetchers,
  nix-flake,
  cmake,
  ninja,
  pyproject-nix,
  pyprojectVersionPatchHook,
  version,
  sanitizer ? null,
  sanitizerRuntime ? null,
  # The distribution name to publish under, or null to build for this
  # repository. Setting it makes the build **publishable to PyPI**, and it
  # changes two things together because one without the other is not
  # publishable:
  #
  # 1. The distribution takes this name. PyPI holds one name for one project,
  #    and this project builds one artifact for each Nix version, so the Nix
  #    version goes in the name: `nanopynix-bindings-nix2-34`. Each one imports
  #    as `nanopynix_bindings`, so they are alternatives rather than a set to
  #    install together.
  # 2. The `+nix<version>` local segment goes. PEP 440 permits a local version
  #    and **a public index refuses one**, so a wheel that carries it cannot be
  #    uploaded whatever else is right about it.
  #
  # Both stay for a build of this repository, where the local segment is the
  # right answer: it keeps two builds of one source apart, it orders the way a
  # reader expects, and it takes the version of the git scope, which no public
  # version can spell.
  pypiName ? null,
  # Build the extension against the stable ABI of CPython, and tag the wheel
  # `cp313-abi3`. **For the wheel, and for nothing that Nix builds.**
  #
  # One such wheel imports on 3.13, 3.14 and every CPython after them. Without
  # it PyPI needs one wheel for each Python minor version, times three Nix
  # versions and two architectures, and each release of CPython rebuilds all of
  # them. `nanopynix-bindings/CMakeLists.txt` gives the count.
  #
  # A package that Nix builds is built for the one interpreter that Nix builds
  # it against, so the compatibility buys nothing there, and nanobind states a
  # cost: the stable ABI stops it reading the internals of the data structures
  # of CPython directly. `pynix` and the test suite therefore take the ordinary
  # build.
  stableAbi ? false,
}:

let
  # Use latest nanobind (2.13.0 26/07/09) because it's fixed a stub generation bug
  nanobind2_13 = nanobind.overrideAttrs (
    final: prev: {
      version = "2.13.0";
      src = prev.src.override {
        hash = "sha256-YAqjcVBkuNsXvrAaVmDRLQ1F38UBqdnIf8+OseNBzG4=";
      };
    }
  );
in
let
  attrs =
    (pyproject-nix.lib.project.loadPyproject { projectRoot = toString ./.; })
    .renderers.buildPythonPackage
      { inherit python; };
in
buildPythonPackage (
  attrs
  // {
    # PEP 440 local version identifier: `<our version>+nix<nix version>`, so
    # two builds of the same source against different Nix components stay
    # distinguishable *and* parse. The old `-` form did not -- `0.1.0-2.35.1`
    # is not a version at all under PEP 440 (a `-` separator is only legal
    # before a post/pre/dev segment, so `0.1.0-2` parses as `0.1.0.post2` and
    # `0.1.0-2.35.1` is rejected outright). That went unnoticed until nixpkgs
    # added pythonMetadataCheckHook, which parses both the derivation version
    # and the one in `.dist-info/METADATA` with `packaging`.
    #
    # The local segment is the right place for it: it keeps the public
    # version `0.1.0` (so dependency specifiers on this project still work),
    # it orders the way you would want between Nix versions, and PEP 440
    # normalisation accepts the git scope's version too
    # (`2.35pre20260619_f8bb823a` -> local `nix2.35pre20260619.f8bb823a`).
    #
    # A publishable build drops it, because a public index refuses a local
    # version. `pypiName` above says why the name carries the Nix version
    # instead.
    version = if pypiName == null then "${attrs.version}+nix${version}" else attrs.version;

    # `pname` follows the distribution name, so the store path and the wheel
    # agree on what this package is called.
    pname = if pypiName == null then attrs.pname else pypiName;

    src = ./.;

    build-system = attrs.build-system ++ [
      cmake
      ninja
    ];

    nativeBuildInputs = [
      pkg-config
    ]
    # Rewrites `project.version` in pyproject.toml to the derivation's
    # `version` before the build reads it, so the wheel's METADATA carries the
    # `+nix<version>` local segment too. Without it the build is still correct
    # but pythonMetadataCheckHook fails the derivation, because
    # scikit-build-core would emit the unsuffixed `0.1.0` from the checkout
    # while the derivation claims the suffixed one.
    #
    # **A publishable build has no version to patch**, and this hook treats
    # that as an error rather than as nothing to do: `The version in
    # pyproject.toml already matches the derivation's version. Remove
    # pyprojectVersionPatchHook.`
    ++ lib.optional (pypiName == null) pyprojectVersionPatchHook;

    # `pyprojectVersionPatchHook` rewrites `project.version` and never the
    # name, so a publishable build rewrites the name here. Without it the build
    # is named `nanopynix-bindings-nix2-34` and the wheel inside it still says
    # `nanopynix_bindings-0.1.0-...`, which is the name that PyPI reads.
    #
    # `--replace-fail`, so a rename of the project in `pyproject.toml` stops
    # this build rather than quietly publishing under the old name.
    postPatch = lib.optionalString (pypiName != null) ''
      substituteInPlace pyproject.toml \
        --replace-fail 'name = "nanopynix-bindings"' 'name = "${pypiName}"'
    '';
    # nix's modular components don't propagatedBuildInputs their own C
    # library deps (blake3, boost, libarchive, libsodium, ...) to consumers,
    # so pkg-config can't find e.g. libblake3.pc for nix-util unless it's
    # walked out of nix-util's own buildInputs here, recursively (a
    # transitive dep can itself pull in another).
    buildInputs =
      let
        recursivePropagation =
          derivations:
          lib.concatMap (
            x:
            if x.buildInputs or null != null then
              [ x ] ++ x.buildInputs ++ recursivePropagation x.buildInputs
            else
              [ ]
          ) derivations;
        recdep = [
          nix-util
          nix-store
          nix-expr
          nix-fetchers
          nix-flake
        ];
      in
      lib.unique ([ nanobind2_13 ] ++ recdep ++ (recursivePropagation recdep));

    dontUseCmakeConfigure = true;

    cmakeFlags = [
      "-Dnanobind_ROOT=${nanobind2_13}/${python.sitePackages}/nanobind/cmake"
      "-DPython_EXECUTABLE=${python}/bin/python"
    ]
    ++ lib.optional stableAbi "-DNANOPYNIX_STABLE_ABI=ON"
    ++ lib.optionals (sanitizer != null) [
      # Each cmakeFlags entry becomes its own -Ccmake.args= token, and a single
      # entry with embedded spaces gets re-split upstream into bare (invalid)
      # CMake arguments -- so only a space-free flag can go here. That rules
      # out CMAKE_CXX_FLAGS, which needs the whole space-separated set: it is a
      # plain string variable, so joining with ";" would put a literal
      # semicolon on the compile line. The compile flags go through
      # NIX_CFLAGS_COMPILE below instead, which is a string and takes spaces.
      #
      # RelWithDebInfo already implies -g; frame-pointer retention isn't
      # essential for the DWARF-based unwinding either sanitizer does.
      "-DCMAKE_EXE_LINKER_FLAGS=${sanitizer.linkFlag}"
      "-DCMAKE_SHARED_LINKER_FLAGS=${sanitizer.linkFlag}"
      "-DCMAKE_BUILD_TYPE=RelWithDebInfo"
    ];

    dontStrip = sanitizer != null;

    # postInstall's stubgen and the pythonImportsCheck phase both dlopen()
    # these .so's into a fresh, plain python process; the sanitizer runtime
    # must be preloaded before that process does anything else, or its
    # static-TLS setup fails ("cannot allocate memory in static TLS block")
    # since a late dlopen() can't grow the TLS block CPython already sized at
    # startup. Setting it derivation-wide covers both phases uniformly.
    #
    # `sanitizerRuntime` is null for the UBSan variant, so neither phase gets a
    # preload there. That variant needs none: the link of the extension records
    # its runtime as a dependency, and the loader brings the runtime in.
    # nix/sanitizer.nix gives the whole reason.
    #
    # **A preload that reaches every phase also reaches `bash`.** The ASAN
    # variant needs `ASAN_OPTIONS=detect_leaks=0` for that reason alone, or
    # LeakSanitizer reports the build shell and the build fails. That value
    # comes from `sanitizer.buildEnv`, which gives the measurement.
    env =
      {
        # **The type information of this extension keeps default visibility.**
        # Without this flag a `catch` and a `dynamic_cast` across the boundary
        # between this extension and `libnixexpr.so` both fail.
        #
        # nanobind compiles the extension with `-fvisibility=hidden`. A class
        # that a `catch` clause names then gets a *local* `type_info` here,
        # which the loader cannot merge with the copy that `libnixexpr.so`
        # exports. libstdc++ falls back to comparing the two by name and still
        # matches. The libc++abi that zig links compares them by address, so it
        # never matches, and the zig build broke in two ways at once:
        #
        # 1. Every Nix error reached Python as `SystemError`, "exception could
        #    not be translated". No clause of `src/nix_errors.cpp` matched --
        #    not `catch (nix::Error &)`, and not nanobind's own
        #    `catch (std::exception &)`.
        # 2. `dynamic_cast` returned null, so `find_roots()` on a real
        #    `LocalStore` answered "store 'local://' does not support garbage
        #    collection". **A wrong answer, and not an error.**
        #
        # `-fvisibility-ms-compat` sets the default visibility of a value to
        # hidden and of a type to default. nanobind already hides the values,
        # so this changes the types alone. Measured on two shared objects, one
        # throwing and one catching, with the catching one `dlopen`ed
        # `RTLD_LOCAL` the way CPython opens an extension:
        #
        #   -fvisibility=hidden                          translation lost
        #   -fvisibility=hidden -fvisibility-ms-compat   correct
        #   -fvisibility=default                         correct
        #   gcc, -fvisibility=hidden                     correct
        #
        # `-D_LIBCPP_TYPEINFO_COMPARISON_IMPLEMENTATION=3` does **not** correct
        # it: that macro changes an inline comparison in `<typeinfo>`, and the
        # comparison that decides a `catch` is inside the libc++abi that zig
        # already built.
        #
        # **Here, and not in `nix/zig-stdenv.nix`.** On the whole closure the
        # flag hides the API of every C library that exports by default
        # visibility. `attr` was the first to stop: `libattr.so` built, and the
        # `attr` tool beside it could not link against it
        # (`undefined symbol: attr_get`).
        #
        # It applies to the gcc build as well. That build is correct without
        # it, because libstdc++ compares by name, but one flag for both keeps
        # the two builds the same shape.
        #
        # cc-wrapper appends `NIX_CFLAGS_COMPILE` after the command line that
        # cmake writes, so this wins over nanobind's `-fvisibility=hidden`.
        NIX_CFLAGS_COMPILE = lib.concatStringsSep " " (
          [ "-fvisibility-ms-compat" ]
          # The same flags every nix-* component gets (nix/sanitizer.nix), so
          # the extension and the libraries it calls agree about
          # instrumentation.
          ++ lib.optional (sanitizer != null) sanitizer.flags
        );
      }
      // lib.optionalAttrs stableAbi {
        # **The tag of the wheel, which CMake does not decide.** `STABLE_ABI`
        # gives the extension the `.abi3.so` suffix, and scikit-build-core
        # still writes `cp314-cp314` on the wheel unless it is told. A wheel
        # with the interpreter tag installs on one CPython whatever the
        # extension inside it supports, so the two settings are one change.
        #
        # `cp313`, and not `cp312`: `pyproject.toml` says
        # `requires-python = ">=3.13"`, and a tag below that floor would
        # promise a Python that this project does not support.
        SKBUILD_WHEEL_PY_API = "cp313";
      }
      // lib.optionalAttrs (sanitizer != null) sanitizer.buildEnv
      // lib.optionalAttrs (sanitizerRuntime != null) {
        LD_PRELOAD = sanitizerRuntime;
      };

    # One `.pyi` per area, exactly as when each area was its own extension
    # module -- so `nanopynix_bindings.expr` still resolves to `expr.pyi` for a
    # type checker even though there is no `expr` file on disk any more. A stub
    # with no source counterpart is fine in a `py.typed` package.
    #
    # `-o <file>` rather than `-O <dir>`: stubgen infers the output name from
    # the module's `__file__`, and a submodule of an extension has none. It
    # says so and exits rather than guessing, which is how this was found.
    postInstall = ''
      _site="$out/${python.sitePackages}"
      for mod in errors signals util store expr fetchers flake; do
        _pat=""
        if [ -f "src/$mod.pat" ]; then
          _pat="-p src/$mod.pat"
        fi
        PYTHONPATH="$_site:$PYTHONPATH" \
          ${python}/bin/python -m nanobind.stubgen -m "nanopynix_bindings.$mod" $_pat -o "$_site/nanopynix_bindings/$mod.pyi"
      done
      touch "$_site/nanopynix_bindings/py.typed"
    '';

    pythonImportsCheck = [
      "nanopynix_bindings.errors"
      "nanopynix_bindings.signals"
      "nanopynix_bindings.util"
      "nanopynix_bindings.store"
      "nanopynix_bindings.expr"
      "nanopynix_bindings.fetchers"
      "nanopynix_bindings.flake"
    ];

    # `nix/wheel.nix` gates the suffix of the extension against this, so the
    # flag travels with the derivation and the two cannot disagree. An
    # `override` argument is not an attribute of the result, so it has to be
    # said here.
    passthru = (attrs.passthru or { }) // {
      inherit stableAbi;
    };

    meta = attrs.meta // {
      license = lib.licenses.lgpl21Plus;
      platforms = lib.platforms.unix;
    };
  }
)
