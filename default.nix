let
  flake = (import ./nix/compat.nix);
in
{
  inputs ? flake.inputs,
  system ? builtins.currentSystem,
  pkgs ? (inputs.nixpkgs.legacyPackages.${system}),
}:
let
  pkgs' = pkgs.extend (
    final: prev: {
      python315 = prev.python315.override {
        packageOverrides = pySelf: pyPrev:
          let
            # Only apply the override when the package is older than the
            # threshold that is known to fix Python 3.15. Traces when the
            # condition no longer holds so the override can be removed.
            overrideIfOlder =
              pkg: threshold: overrideAttrs:
              if prev.lib.versionOlder pkg.version threshold then
                pkg.overridePythonAttrs overrideAttrs
              else
                builtins.trace "default.nix: ${pkg.pname} ${pkg.version} >= ${threshold}, remove conditional override"
                  pkg;

            # Helper for the common case of disabling checks on 3.15 --
            # wraps the same version gate so we get a trace when nixpkgs
            # updates past the threshold.
            disableTestsIfOlder =
              pkg: threshold:
              overrideIfOlder pkg threshold (_old: {
                doCheck = false;
                doInstallCheck = false;
              });
          in
          {
            tkinter = disableTestsIfOlder pyPrev.tkinter "99.0.0";

            beartype = overrideIfOlder pyPrev.beartype "99.0.0" (old: {
              patches = (old.patches or [ ]) ++ [ ./nix/patches/beartype-py315.patch ];
              pytestFlags = (old.pytestFlags or [ ]) ++ [
                "-W"
                "ignore::DeprecationWarning"
              ];
              disabledTests = (old.disabledTests or [ ]) ++ [
                "test_api_typing"
                "test_claw_intraprocess_beartype_this_package"
                "test_claw_intraprocess_beartype_package"
                "test_claw_intraprocess_beartype_packages"
                "test_claw_intraprocess_beartype_all"
                "test_claw_intraprocess_beartyping"
                "test_claw_intraprocess_decorator_hostile"
                "test_claw_extraprocess_executable_submodule"
                "test_claw_extraprocess_executable_package"
                "test_claw_unoptimized"
              ];
            });

            exceptiongroup = overrideIfOlder pyPrev.exceptiongroup "99.0.0" (_old: {
              disabledTests = [ "test_nameerror_suggestions_in_group" ];
            });

            pure-eval = overrideIfOlder pyPrev.pure-eval "99.0.0" (_old: {
              disabledTests = [
                "test_eval_attrs"
                "test_is_expression_interesting"
                "test_basic"
                "test_class_as_property"
                "test_custom_object_dict"
                "test_descriptor"
                "test_inherited"
                "test_inherited_slots"
                "test_instance_attr"
                "test_metaclass_dict_as_property"
                "test_mro_as_property"
              ];
            });

            time-machine = overrideIfOlder pyPrev.time-machine "3.4.0" (old: {
              version = "3.4.0";
              src = prev.fetchFromGitHub {
                owner = "adamchainz";
                repo = "time-machine";
                tag = "3.4.0";
                hash = "sha256-9ocj5RsjmHtXjcueDJE4v9QvpeFXgPSNam1Wct0q89o=";
              };
              disabledTests = (old.disabledTests or [ ]) ++ [
                "test_date_today"
                "test_localtime_and_gmtime_match_datetime"
              ];
              nativeCheckInputs = (old.nativeCheckInputs or [ ]) ++ [ pySelf.hypothesis ];
            });

            setproctitle = overrideIfOlder pyPrev.setproctitle "99.0.0" (_old: {
              disabledTests = [ "test_clear_segfault" ];
            });

            parso = overrideIfOlder pyPrev.parso "99.0.0" (_old: {
              disabledTests = [ "test_python_exception_matches" ];
            });

            jedi = overrideIfOlder pyPrev.jedi "99.0.0" (old: {
              disabledTests = (old.disabledTests or [ ]) ++ [
                "test_python_exception_matches"
                "test_find_system_environments"
                "test_scanning_venvs"
                "test_create_environment_venv_path"
                "test_create_environment_executable"
                "test_venv_and_pths"
              ];
            });

            meson-python = overrideIfOlder pyPrev.meson-python "99.0.0" (old: {
              disabledTests = (old.disabledTests or [ ]) ++ [ "test_tag_stable_abi" ];
              disabledTestPaths = (old.disabledTestPaths or [ ]) ++ [ "tests/test_wheel.py::test_limited_api" ];
            });

            hypothesis = overrideIfOlder pyPrev.hypothesis "99.0.0" (old: {
              disabledTests = (old.disabledTests or [ ]) ++ [
                "test_interactive_example_does_not_emit_warning"
                "test_given_does_not_pollute_state"
                "test_find_does_not_pollute_state"
                "test_prints_seed_only_on_healthcheck"
                "test_does_print_on_reuse_from_database"
                "test_prints_seed_on_very_slow_shrinking"
                "test_regex_output_should_print_as_string"
              ];
              disabledTestPaths = (old.disabledTestPaths or [ ]) ++ [ "tests/cover/test_lookup.py" ];
            });

            rich = overrideIfOlder pyPrev.rich "99.0.0" (old: {
              disabledTests = (old.disabledTests or [ ]) ++ [
                "test_inspect_builtin_function_except_python311"
                "test_inspect_builtin_function_only_python311"
                "test_inspect_integer_with_methods_python38_and_python39"
                "test_inspect_integer_with_methods_python310only"
                "test_inspect_integer_with_methods_python311"
                "test_attrs_broken"
              ];
            });

            fonttools = overrideIfOlder pyPrev.fonttools "99.0.0" (old: {
              disabledTestPaths = (old.disabledTestPaths or [ ]) ++ [
                "Tests/metaTools/check_table_coverage_test.py"
              ];
            });

            zlib-ng = overrideIfOlder pyPrev.zlib-ng "99.0.0" (old: {
              disabledTestPaths = (old.disabledTestPaths or [ ]) ++ [
                "tests/test_zlib_compliance.py"
              ];
            });

            mypy = overrideIfOlder pyPrev.mypy "2.3.1" (old: {
              version = "2.3.1";
              src = prev.fetchPypi {
                pname = "mypy";
                version = "2.3.1";
                hash = "sha256-R8GxIHJYUTqdk0lfaci+nec5FhhvDlJwPoxGG3piNBk=";
              };
            });

            blockbuster = overrideIfOlder pyPrev.blockbuster "1.5.27" (old: {
              version = "1.5.27";
              src = prev.fetchPypi {
                pname = "blockbuster";
                version = "1.5.27";
                hash = "sha256-uOnZiLm5G6RoyUUw4hnyagDT/2FrOevz2lYaKj7qndQ=";
              };
              doCheck = false;
              doInstallCheck = false;
            });

            typeguard = overrideIfOlder pyPrev.typeguard "4.6.0" (old: {
              version = "4.6.0";
              src = prev.fetchPypi {
                pname = "typeguard";
                version = "4.6.0";
                hash = "sha256-50FPCRETF94+M13pLNOXxcDKALHMFnbeEuHURKebPyE=";
              };
            });

            astroid = overrideIfOlder pyPrev.astroid "4.3.1" (old: {
              version = "4.3.1";
              src = prev.fetchPypi {
                pname = "astroid";
                version = "4.3.1";
                hash = "sha256-uzWSU9jO1mNaOIHBfry7wOC2XKI7VVqb0DySo8v0yqc=";
              };
              disabledTests = (old.disabledTests or [ ]) ++ [
                "test_typed_dict_required_and_optional_keys"
              ];
            });

            executing = overrideIfOlder pyPrev.executing "99.0.0" (old: {
              disabledTests = (old.disabledTests or [ ]) ++ [
                "test_iter"
                "test_with"
                "test_small_samples"
              ];
            });

            stack-data = overrideIfOlder pyPrev.stack-data "99.0.0" (_old: { });

            pydantic = overrideIfOlder pyPrev.pydantic "99.0.0" (old: {
              disabledTests = (old.disabledTests or [ ]) ++ [
                "test_base64url"
                "test_base64url_invalid"
              ];
            });

            ipython = overrideIfOlder pyPrev.ipython "9.16.1" (old: {
              version = "9.16.1";
              src = prev.fetchPypi {
                pname = "ipython";
                version = "9.16.1";
                hash = "sha256-Wj0fmkf/IW1s+c+GMST2osGhmNE1TFRqTSSjcKKDtkw=";
              };
            });

            uvloop = overrideIfOlder pyPrev.uvloop "99.0.0" (old: {
              disabledTests = (old.disabledTests or [ ]) ++ [ "test_socket_sync_remove" ];
            });

            anyio = overrideIfOlder pyPrev.anyio "99.0.0" (_old: { });

            aiohttp = disableTestsIfOlder pyPrev.aiohttp "99.0.0";

            twisted = disableTestsIfOlder pyPrev.twisted "99.0.0";

            django = disableTestsIfOlder pyPrev.django "99.0.0";

            datamodel-code-generator = overrideIfOlder pyPrev.datamodel-code-generator "0.74.0" (old: {
              version = "0.74.0";
              src = prev.fetchPypi {
                pname = "datamodel_code_generator";
                version = "0.74.0";
                hash = "sha256-2wmY2SDndPSEQsonArkBBJvmDDXMtUcIbFdLHSYfFEQ=";
              };
              patches = (old.patches or [ ]) ++ [ ./nix/patches/datamodel-code-generator-py315.patch ];
              __darwinAllowLocalNetworking = true;
              nativeCheckInputs = (old.nativeCheckInputs or [ ]) ++ [ pySelf.trustme ];
              disabledTests = (old.disabledTests or [ ]) ++ [
                "test_check_overview_sync"
                "test_related_page_tags_prefer_existing_generated_section"
                "test_focused_topics_nav_matches_option_topics"
                "test_build_schema_docs"
                "test_build_architecture_docs"
                "test_build_conformance_docs"
                "test_build_deprecation_docs"
                "test_build_docs_examples"
                "test_build_experimental_docs"
                "test_build_llms_txt"
                "test_build_preset_docs"
                "test_build_release_benchmark_docs"
                "test_build_playground_assets"
                "test_main_kr"
                "test_input_model"
                "test_skill"
                "test_cli_doc"
                "test_https"
              ];
              disabledTestPaths = (old.disabledTestPaths or [ ]) ++ [
                "tests/main"
              ];
            });

            scikit-build-core = overrideIfOlder pyPrev.scikit-build-core "1.0.3" (old: {
              version = "1.0.3";
              src = prev.fetchPypi {
                pname = "scikit_build_core";
                version = "1.0.3";
                hash = "sha256-pNegWXjuN5dcN3Q1EMiZHi3rzn74OvsKB8DFdv1PFug=";
              };
              disabledTests = (old.disabledTests or [ ]) ++ [ "test_abi3_wheel" ];
            });

            # PyO3 0.26 does not support Python 3.15 -- override with
            # ABI3 forward-compat flag until libcst updates its Cargo.lock
            # to PyO3 >= 0.29. Threshold is the first libcst version that
            # bumps PyO3 past 0.29.
            libcst = overrideIfOlder pyPrev.libcst "1.9.0" (_old: {
              env.PYO3_USE_ABI3_FORWARD_COMPATIBILITY = 1;
            });

            # Example of a version-gated bump to a newer upstream that
            # fixes 3.15 (replace threshold and src when needed):
            # somepkg = overrideIfOlder pyPrev.somepkg "2.0.0" (_old: {
            #   version = "2.0.0";
            #   src = prev.fetchPypi { ... };
            # });
          };
      };

      nanopython = final.python315;

      graphite2 = prev.graphite2.override {
        python3 = final.nanopython;
      };

      pythonPackagesExtensions = prev.pythonPackagesExtensions ++ [
        (
          python-final: python-prev: {
            # THE ONE OVERRIDE, and one reason for it now.
            #
            # **The five disabled tests are gone, because nixpkgs disables them
            # itself.** They were the ruff ones: the package runs ruff over the code
            # it generates and compares the result against a checked-in
            # expectation, and a newer ruff writes a blank line after
            # `from __future__ import annotations`. nixpkgs PR #548078 merged on
            # 2026-08-04 as `37fc74a8`, and the nixpkgs this repository pins,
            # `8be7bd0c`, is 6695 commits after it and none behind. Issue #49.
            #
            # Six of its tests start a real HTTP server on loopback, and the
            # Darwin build sandbox refuses the `bind`:
            #
            #   socketserver.py:478: PermissionError: [Errno 1] Operation not permitted
            #
            # 5948 pass and only these six fail, so this is the sandbox saying no
            # rather than the package being broken. `__darwinAllowLocalNetworking`
            # is nixpkgs' own switch for exactly that, and it is ignored on Linux,
            # so it costs nothing there and keeps the six tests running here
            # instead of deleting the coverage.
            #
            # Found making `ekn` build on macOS at all: this package reaches the
            # tree through tree-sitter-config, so those six failures took out the
            # whole toolchain.
            #
            # This stays up here rather than moving to the 3.15-only block
            # below, because the sandbox issue exists on every Python version.
            datamodel-code-generator = python-prev.datamodel-code-generator.overridePythonAttrs (_old: {
              __darwinAllowLocalNetworking = true;
            });

            # The protobuf runtime, and the protoc plugin that writes the modules
            # `nanopynix-proto` and `greeter-proto` are made of. Neither is in
            # nixpkgs. Both used to arrive through the `grpclib-transports` flake
            # input; that input is gone and the project it named is vendored, so
            # its two private dependencies are vendored here beside it.
            #
            # These stay in the interpreter's own set rather than moving to the
            # builders set with `grpclib-transports` itself, because both are
            # reached the nixpkgs way: `python.withPackages` builds the protoc
            # plugin environment in each `generated.nix`, and pyproject.nix's
            # builders deliberately do not propagate, which is the whole reason
            # that generation happens outside the package.
            #
            # They also stay here rather than moving to the 3.15-only block,
            # because every Python version this repo might use needs them.
            betterproto2 = python-final.callPackage ./nix/betterproto2.nix { };
            betterproto2-compiler = python-final.callPackage ./nix/betterproto2-compiler.nix { };

            kr8s = python-final.callPackage ./nix/kr8s.nix { };

            tree-sitter-nix = python-final.callPackage ./nix/tree-sitter-nix.nix {
              # This set, and not `python.pkgs`, which is the set from before
              # these overrides ran. nix/tree-sitter-nix.nix gives the
              # measurement.
              pythonPackages = python-final;
              # `pkgs.path` (the nixpkgs source tree) would otherwise be shadowed
              # by the Python set's own PyPI package literally named "path" --
              # passing `pkgs.path` explicitly sidesteps that entirely.
              nixpkgsPath = prev.path;
              # Same shadowing problem: the set's `tree-sitter` is the PyPI
              # bindings package, not pkgs.tree-sitter (the CLI derivation, whose
              # passthru has `buildGrammar`).
              treeSitterCli = prev.tree-sitter;
              treeSitterNixSrc = inputs.tree-sitter-nix-numtide;
            };
          }
        )
      ];
    }
  );
in
let
  pkgs = pkgs';
  inherit (pkgs) lib;

  pyproject-nix = import "${inputs.pyproject-nix}" { inherit lib; };

  nanopython = pkgs.nanopython;
  nanoPythonPackages = nanopython.pkgs;
  pythonBase = nanopython;

  # Exports OpenTofu's built-in ("core") HCL block schema
  # (resource/data/count/for_each/lifecycle/...) as JSON for a given OpenTofu
  # version, on demand -- see tools/tofu-core-schema/package.nix and
  # pynix-lsp/src/pynix_lsp/_tofu_core_schema.py, which invokes this at LSP-
  # server runtime rather than baking a static snapshot. Independent of any
  # nanopynix/Nix version, so it lives here rather than inside
  # nanopynixForNixVersions.
  tofuCoreSchemaTool = pkgs.callPackage ./tools/tofu-core-schema/package.nix { };

  # Execs a program out of a *relocated* store with that store mounted at its
  # own logical path, the way `nix run` does -- see tools/store-exec/store-exec.c
  # for why this cannot be a binding and has to be a separate exec-final
  # binary. A no-op `execvp` when the store is not relocated, so callers route
  # through it unconditionally. Like tofuCoreSchemaTool it depends on no Nix
  # library, so it lives out here rather than in nanopynixForNixVersions.
  storeExecTool = pkgs.callPackage ./tools/store-exec/package.nix { };

  # **The same tool, as a list that is empty off Linux.** `store-exec.c`
  # rearranges the mount table and the package links `glibc.static`, so
  # `meta.platforms` is `lib.platforms.linux` and that is right. Forcing the
  # derivation on Darwin therefore throws from `check-meta.nix`, and it took
  # the whole test runner with it: the macOS job of #143 refused to evaluate
  # before it built anything.
  #
  # Nix is lazy, so binding `storeExecTool` above costs nothing on Darwin.
  # Only a list position forces it, and every consumer uses one, so the
  # `lib.optional` here is the single place that decides. `storeExecTool`
  # stays exported unchanged, for a consumer outside this repository that
  # already names it.
  storeExecTools = lib.optional pkgs.stdenv.hostPlatform.isLinux storeExecTool;

  # The completion spike: a tiny cyclopts program, the shell code that gives a
  # cyclopts script a dynamic completion, and a pty driver that proves it in
  # fish, bash and zsh. Version-independent, like the two tools above, so it
  # lives out here and not in `nanopynixForNixVersions`. Its own tests run in
  # its own build -- see nix/completion-spike.nix.
  completionSpike = pythonBase.pkgs.callPackage ./nix/completion-spike.nix { };

  # This repo's seam onto pyproject.nix's builders: `ps` builds package sets
  # (nix/python-set.nix), `mkApp` turns one of their packages into a release
  # application (nix/mk-app.nix).
  ps = pkgs.callPackage ./nix/python-set.nix { inherit pyproject-nix; };
  mkApp = pkgs.callPackage ./nix/mk-app.nix {
    pyprojectUtil = pkgs.callPackage pyproject-nix.build.util { };
  };

  # One entry per sanitizer variant that gets its own set of Nix builds.
  #
  # **The ASAN variant runs against a libexpr with no collector, and it is the
  # only one that must.** libexpr refuses the combination of ASAN and the
  # collector, and `nix/sanitizer.nix` gives the test that libexpr makes. An
  # earlier variant reached a build only because the flag arrived in an
  # environment variable that meson never reads, and what that build reported
  # was a tag read of a `Value` that the collector had already freed.
  # `sanitizer.requiresNoGC` now carries that rule, and
  # `nanopynixForNixVersions` asserts on it.
  #
  # UBSan runs on its own rather than beside TSAN, although the two combine.
  # Each one then reports against a build that the other did not instrument,
  # so a finding names one sanitizer and not a pair of them.
  sanitizers = {
    tsan = pkgs.callPackage ./nix/sanitizer.nix { name = "thread"; };
    ubsan = pkgs.callPackage ./nix/sanitizer.nix { name = "undefined"; };
    asan = pkgs.callPackage ./nix/sanitizer.nix { name = "address"; };
  };

  # Every build with a collector links this one. `nix/boehmgc.nix` gives the
  # abort it corrects, and the reason the correction is not a sanitizer
  # concern.
  boehmgc = pkgs.callPackage ./nix/boehmgc.nix { };

  # The collector every build gets, whether or not a sanitizer is asking.
  # `applyBoehmGCPatch` puts it in place for a build with no sanitizer;
  # `boehmgcOverride` is what the sanitized builds add their instrumentation
  # to.
  patchedBoehmGC = boehmgc.patchBoehmGC pkgs.nixDependencies.boehmgc;

  # Every C and C++ library of the closure, rebuilt for the wheel. The file
  # gives the package list, the payload trim and the corrections that each
  # package needs.
  #
  # **At the top level, and not in the `let` of `nanopynixForNixVersions`.**
  # `nanopynixWheel` reads `wheelLibs` to name the licence of each library that
  # rides in the wheel, and that binding is a sibling of this one. Neither this
  # import nor `patchedBoehmGC` above reads an argument of that function, so
  # both moved out whole.
  wheelNix = import ./nix/nix-closure.nix {
    inherit lib pkgs nanopython;
    boehmgc = patchedBoehmGC;
  };

  # A confirmed data race in nix::Bindings::emptyBindings (a process-wide
  # shared static that ExprAttrs::eval unconditionally writes to -- see the
  # patch's own commentary) found via ThreadSanitizer (see `sanitizers` above).
  # thread_local gives each evaluator OS thread its own instance, fixing the
  # race without any behavior change for single-threaded use.
  emptyBindingsPatch = ./nix/patches/nix-thread-local-empty-bindings.patch;

  # The base environment of an evaluator holds one slot for each name that
  # `createBaseEnv` registers, and `BASE_ENV_SIZE` fixes the count at 128 with
  # no bound check on either write. Nix itself needs 119 of those slots, and
  # `nanopynix.register_primop` takes one more for each primop a consumer adds
  # -- 24 of them in `tests/conftest.py` alone, which writes 120 bytes past the
  # end of the block.
  #
  # **Every version gets this, and not only the builds with no collector.**
  # Boehm rounds an allocation up to a size class, so the collector build makes
  # the same out-of-bounds write into the slack of the block and reports
  # nothing. `-Dgc=disabled` gets an exact `calloc`, which is why ASAN found it
  # (issue #52). The defect is in both builds.
  #
  # The patch header gives the measurement, and the reason the size is a
  # constant at all.
  baseEnvSizePatch = ./nix/patches/nix-base-env-size.patch;

  # `gmtime` returns a pointer into one buffer that the C library shares
  # between every thread, and Nix calls it at two places that both format a
  # `lastModified`: `describe` in libflake, which `lockFlake` reaches, and
  # `emitTreeAttrs` in libexpr, which `callFlake` and the `fetchTree` primops
  # reach. Two evaluator threads that touch a flake at the same time then
  # overwrite each other's result. `nix` the command evaluates on one thread
  # and never meets this; nanopynix gives each evaluator its own thread, so it
  # does. ThreadSanitizer found it -- see issue #90, and the patch header.
  #
  # One file, since 2.31 left the matrix. That version needed a second copy of
  # this patch: `describe` was byte-identical everywhere, and `emitTreeAttrs`
  # was not, because 2.31 wrote the call on one line and took no `state.mem`.
  gmtimePatch = ./nix/patches/nix-gmtime-not-thread-safe.patch;

  # `EvalState::printStatistics` writes its report to stderr, or to the file
  # that `NIX_SHOW_STATS_PATH` names. nanopynix embeds the evaluator, so it can
  # read neither. The patch splits a `statisticsJSON` out of that function,
  # turns `count-calls` into an ordinary `EvalSettings` option, and makes the
  # three call-count maps concurrent. The patch header gives the reason for
  # each part, and the upstream defect that the third part corrects.
  #
  # Two files for one change, because the line numbers differ. The 2.35 file
  # covers git as well: `lib.versions.majorMinor` reads git's
  # "2.35pre20260619_f8bb823a" as "2.35", and the patch applies there with an
  # offset and no fuzz.
  countCallsPatch234 = ./nix/patches/nix-2.34-count-calls.patch;
  countCallsPatch235 = ./nix/patches/nix-2.35-count-calls.patch;

  # Which patches to apply to a given nix version's modular component set,
  # keyed by that version's own major.minor (e.g. "2.34"), with `default`
  # as the fallback for anything without its own entry (git's rolling
  # pre-release version string, any future point release, ...).
  nixPatches = {
    # A version with no entry of its own. The countCalls patch is the 2.35
    # file, which is the newest one. A future version whose source moved fails
    # here, at the patch, and that is the failure to want: the bindings gate
    # the statistics on the version number, so a silent absence would instead
    # break the build of the bindings.
    default = [
      emptyBindingsPatch
      baseEnvSizePatch
      gmtimePatch
      countCallsPatch235
    ];
    "2.34" = [
      emptyBindingsPatch
      baseEnvSizePatch
      gmtimePatch
      countCallsPatch234
    ];
    # git resolves to this entry too, because its version string reads as
    # "2.35". The countCalls patch is the 2.35 file for that reason.
    "2.35" = [
      emptyBindingsPatch
      baseEnvSizePatch
      gmtimePatch
      countCallsPatch235
    ];
  };

  patchesFor = scope: nixPatches.${lib.versions.majorMinor scope.version} or nixPatches.default;

  # **The oldest Nix that this repository supports.** Every variant reads it,
  # so one number moves the whole matrix.
  #
  # 2.31 was the version below it, and issue #126 holds the measurement that
  # removed it. On one commit, the `test-local` job skipped 107 tests on 2.31
  # and 13 on 2.35, and it took 12m22s against 7m39s. It was the job with the
  # least signal and the longest run. `ci/render.py` also could not run on 2.31
  # at all, because primop registration is broken there and upstream does not
  # plan to correct it. That second reason no longer holds on its own: issue
  # #121 moved the renderer off `builtins.toYAML`, so it registers no primop.
  #
  # **2.34 and not 2.32, because 2.32 and 2.33 do not exist to build.** nixpkgs
  # carries `nixComponents_2_32` and `nixComponents_2_33`, and evaluating
  # either one throws. `isNixScope` already dropped both, so a floor of "2.32"
  # would select the same versions while naming one that no job can build.
  #
  # Raise this number when the next version earns the same measurement. Do not
  # add a version-specific branch to library code to keep an old one alive --
  # `AGENTS.md` gives that rule, and this floor is what makes it affordable.
  supportedNixFloor = "2.34";

  # The plain Nix package of each supported version, with no patch of this
  # repository on it. `nixFunctionalTests` below runs Nix's own test suite
  # against each one, and that comparison needs the Nix that upstream ships:
  # a patch here would change what the control run measures.
  #
  # `nixVersions` also holds the component scopes, the aliases and the
  # override functions. `isDerivation` drops all three kinds, and the names
  # below are the aliases, which point at a version this set already holds.
  supportedNixPackages = lib.filterAttrs (
    name: value:
    !(builtins.elem name [
      "stable"
      "latest"
      "unstable"
      "minimum"
    ])
    && (builtins.tryEval (lib.isDerivation value && value ? version)).value
    && lib.versionAtLeast (lib.versions.majorMinor value.version) supportedNixFloor
  ) pkgs.nixVersions;

  # Builds one full nanopynix scope per modular Nix component set nixpkgs
  # exposes, optionally with ThreadSanitizer instrumentation applied to nix
  # itself and to nanopynix's own C++ bindings. Rather than hand-enumerating
  # nixComponents_2_34/nixComponents_2_35/nixComponents_git, this discovers
  # every modular component set via isNixScope and extends each with
  # nanopynix's own packages uniformly (using the scope's own
  # newScope/callPackage machinery, so e.g. nanopynix-bindings's
  # nix-store/nix-expr/... args resolve to that version's own components, not
  # some other pkgs-level one) -- nixpkgs adding a new nixComponents_X is
  # picked up here without editing this file. No separate dedup step is
  # needed: isNixScope only matches modular component scopes, never the
  # "stable"/"latest" plain-derivation aliases that could otherwise collide.
  nanopynixForNixVersions =
    # `null` for the plain build, or one of `sanitizers` above. The variants
    # are separate attribute sets rather than an option on one build, because
    # every C and C++ library in the process has to agree.
    {
      sanitizer ? null,
      # Whether libexpr keeps the Boehm collector.
      #
      # **A whole scope, and never a runtime choice.** `EvalState` carries a
      # `baseEnvP` member only when the collector is present, so the two builds
      # are not ABI-compatible and one process must load exactly one of them.
      # Nothing in Python can pick: a forkserver child imports
      # `nanopynix.rpc.worker._worker` while it unpickles the process target,
      # before any code of ours runs there, and `multiprocessing` keeps one
      # forkserver for each process, started from the parent's `sys.path`. So
      # the venv decides, and a non-GC deployment is a different venv.
      #
      # Without the collector the evaluator allocates and never releases. Nix's
      # own `libexpr/package.nix` says why that is tolerable: "this is not as
      # bad as it sounds so long as evaluation just takes place within
      # short-lived processes". An RPC worker is such a process.
      gc ? true,
      # Whether the whole C and C++ closure is rebuilt for a PyPI wheel, which
      # lowers the glibc floor and gives the closure one private C++ runtime so
      # that the wheel runs off NixOS.
      #
      # A whole scope, and for the same reason as the sanitizer above: a wheel
      # takes the highest glibc floor of everything that it carries, so one
      # library of the stdenv of nixpkgs holds the whole wheel at `GLIBC_2.38`.
      # `nix/nix-closure.nix` names each package, and issue #111 holds the
      # measurements.
      wheel ? false,
    }:
    assert lib.assertMsg (!(sanitizer.requiresNoGC or false) || !gc) ''
      The ${sanitizer.name} sanitizer needs `gc = false`.

      libexpr fails the build outright on the combination, rather than
      disabling the collector by itself, because nixpkgs writes
      `-Dgc=enabled` and `disable_if` only demotes an `auto` feature. That
      error arrives after a from-source rebuild of the whole instrumented
      closure, so it is caught here instead.
    '';
    let
      isNixScope =
        _name: v:
        (builtins.tryEval v).success
        && !lib.isDerivation v
        && lib.isAttrs v
        && lib.hasAttr "appendPatches" v
        && lib.hasAttr "overrideScope" v
        && lib.hasAttr "newScope" v
        && lib.hasAttr "packages" v;

      patchNixScope = scope: scope.appendPatches (patchesFor scope);

      # nix's own components (nix-util, nix-store, ...) keep resolving
      # through scope.newScope completely unmodified below -- so their own
      # deps (e.g. nix-util's `brotli`) still come from plain nixpkgs, not
      # from the Python set (which has its own, incompatible `brotli`: the
      # Python bindings, not the C library with a pkg-config .pc file). Our
      # own packages instead go through callNixPythonPackage, a second
      # callPackage-like function that also has `python.pkgs` and pkgs in
      # scope, plus `final` so they can still reference
      # nix-store/nix-expr/nanopynix-bindings/etc directly.
      extendNixScope =
        scope:
        lib.makeScope scope.newScope (
          lib.extends (
            final: _prev:
            let
              sanitizerRuntime = if sanitizer == null then null else sanitizer.runtime;

              # `pythonBase` unchanged. There used to be a per-version overlay
              # here adding our own projects to the interpreter's set, but
              # they are pyproject.nix builders packages now and live in
              # `pythonSet` below. The only per-version Python package left is
              # nanopynix-bindings, and that goes straight into the builders
              # set as a lifted root -- so nothing version-specific needs to
              # be in the interpreter's own set at all.
              python = pythonBase;

              # nanopynix-bindings is the one package left that needs
              # nixpkgs' Python infrastructure spliced in (buildPythonPackage,
              # nanobind, the stub-generation machinery). Everything else in
              # this scope resolves through `final.callPackage`, which is the
              # scope's own -- so `python.pkgs` is not in scope for them, and
              # cannot shadow a builders-set package with the nixpkgs one of
              # the same name.
              callPythonPackage = lib.callPackageWith (
                pkgs
                // python.pkgs
                // {
                  inherit python pyproject-nix;
                }
                // final
              );
            in
            {
              # nanopynix-bindings stays a nixpkgs `buildPythonPackage`: it is
              # a cmake/nanobind extension linked against *this* scope's Nix
              # C++ components, with its own stub generation, and none of that
              # is what pyproject.nix's builders are for. It is lifted into
              # the builders set below instead -- which is exactly what
              # `hacks.nixpkgsPrebuilt` exists for.
              nanopynix-bindings = callPythonPackage ./nanopynix-bindings/package.nix {
                inherit sanitizer sanitizerRuntime;
              };

              # Everything above the bindings is a pyproject.nix builders
              # package. The set is built once per Nix version and holds both
              # the built and the editable form of each project.
              # No sanitizer: nothing in these pure-Python
              # builds loads the instrumented extension, and preloading the
              # TSAN runtime here only instrumented `uv` -- see the comment
              # on the `nanopynix` override in that file.
              pyPackages = pkgs.callPackage ./nix/py-packages.nix {
                inherit
                  ps
                  python
                  ;
                root = ./.;
                # The linked Nix version, so two builds of the same source
                # against different Nix components are distinguishable.
                inherit (final) version;
              };

              /*
                This repo's Python closure, optionally widened by a consumer's
                own projects.

                The parameters exist for a consumer that builds one of *its
                own* pyproject.toml projects against this closure --
                easykubenix does, for its `ekn` CLI. Adding the project to the
                overlay is not enough on its own: a set's nixpkgs packages are
                lifted once, from the roots `mkPythonSet` is seeded with, and
                the lifting machinery is internal to nix/python-set.nix. So a
                dependency that only the consumer's project declares (`kr8s`,
                for `ekn`) has no way into the set after the fact -- an
                `overrideScope` can add the project but not the closure it
                needs. Passing `projectRoots` here reads that project's
                pyproject.toml alongside ours and resolves its dependencies
                the same way, which is the only place that can happen.

                Type: pythonSetWith :: AttrSet -> AttrSet
              */
              pythonSetWith =
                {
                  # Consumer pyproject.toml directories, read for their
                  # third-party dependencies exactly as ours are. Their own
                  # names are excluded from the nixpkgs lookup automatically
                  # (see `nixpkgsRootsFor`), since `overlay` supplies them.
                  projectRoots ? [ ],
                  # The consumer's own projects, as a standard overlay.
                  # Composed *over* ours, so it can also replace one of them.
                  overlay ? (_final: _prev: { }),
                }:
                ps.mkPythonSet {
                  inherit python;
                  # Sourced from nixpkgs: the build systems, plus the whole
                  # third-party runtime closure, plus our own native extension.
                  # `python.pkgs` already resolved every one of those names, so
                  # the roots are just the propagated inputs nixpkgs computed --
                  # no second hand-written dependency list to fall out of date.
                  nixpkgsRoots = [
                    final.nanopynix-bindings
                  ]
                  ++ ps.nixpkgsRootsFor {
                    inherit python;
                    # `completion-spike` is not one of `pyPackages`: it is a
                    # nixpkgs `buildPythonApplication`, and it runs its own
                    # tests in its own build. Its *declarations* are read here
                    # anyway, so that `cyclopts` and `pexpect` reach this set
                    # and the type gate can see the tree. Reading the
                    # pyproject.toml is all `nixpkgsRootsFor` does, so this
                    # adds no second package.
                    projectRoots = final.pyPackages.projectRoots ++ [ ./completion-spike ] ++ projectRoots;
                    # A nixpkgs Python package, but this scope's own -- lifted
                    # in as a root above rather than looked up by name.
                    exclude = [ "nanopynix-bindings" ];
                  };
                  overlay = lib.composeExtensions final.pyPackages.built overlay;
                };

              pythonSet = final.pythonSetWith { };

              # The same set with our projects swapped for editable installs.
              # `mkVirtualEnv` from here gives a venv whose site-packages
              # points back at this checkout.
              editablePythonSet = final.pythonSet.overrideScope final.pyPackages.editable;

              inherit (final.pythonSet)
                nanopynix-proto
                nanopynix-helpers
                # The command-line layer that issue #222 moved out of
                # `pynix`. Exported so that a second Nix CLI in Python can
                # take it instead of copying it, which is the whole reason it
                # is a project of its own.
                libpynix
                # The pytest plugin, developed here alongside everything else.
                # Exported because a consumer's test suite may want it too --
                # see the note on the outer `inherit` for the one way to take
                # it that actually works.
                pytest-agent
                ;

              nanopynix = final.pythonSet.nanopynix // {
                test = final.callPackage ./nanopynix/tests.nix {
                  inherit (final.nanopynix) version;
                  inherit (inputs) nixpkgs;
                  inherit sanitizer sanitizerRuntime;
                  inherit (final) pythonSet;
                  # The one list that the dev shell also takes, so a tool the
                  # suite needs cannot reach only one of them. See
                  # nix/suite-runtime.nix.
                  inherit (final) suiteRuntime;
                };
              };

              pynix = mkApp {
                name = "pynix";
                inherit (final) pythonSet;
                # `pynix develop` calls `nanopynix.store_exec_prefix`, which
                # resolves this off PATH. The prefix runs a program out of a
                # store that is relocated, which is every store that pynix
                # opens away from the root one.
                #
                # `tofuCoreSchemaTool` was here as well until issue #107. It
                # belongs to the language server, so it is on the PATH of the
                # `pynix-lsp` application below.
                pathInputs = storeExecTools;
                completions = true;
              };
              # The language server, as a release application of its own.
              # Issue #107 split it out of `pynix`, so that `pygls`,
              # `lsprotocol` and `jsonschema` are not in the closure of
              # `pynix build`. `pynix` is still a dependency of it, because the
              # server imports `pynix._nix_syntax` and `pynix._completion`.
              #
              # `tofuCoreSchemaTool` is here because
              # `pynix_lsp._tofu_core_schema` runs it at request time, rather
              # than reading a snapshot that this repository stores.
              # `storeExecTools` is here for the same reason it is on `pynix`:
              # the terranix dialect runs `tofu` out of the store that the
              # server evaluates against.
              pynix-lsp = mkApp {
                name = "pynix-lsp";
                inherit (final) pythonSet;
                pathInputs = [
                  tofuCoreSchemaTool
                ]
                ++ storeExecTools;
              };
              # The daemon proxy, as a release application. It needs no
              # `pathInputs`: it speaks the daemon protocol itself and shells
              # out to no tool. Issue #131 added it, so that
              # `nixosModules.pynixd` can default `services.pynixd.package` to
              # a build of this repository rather than to a second, separately
              # pinned one under `pynixd/default.nix`.
              pynixd = mkApp {
                name = "pynixd";
                inherit (final) pythonSet;
              };
              # Nix's own functional test suite, run against a daemon. One
              # program for each supported Nix version, and each program
              # carries its Nix, the test scripts of that Nix, and pynixd. So
              # a person runs two commands and gets a comparison:
              #
              #     nix build --file . nixFunctionalTests.nix_2_34 --out-link result
              #     ./result/bin/nanopynix-nixft-nix_2_34 all
              #
              # The tests build derivations, so they need Linux. Read
              # `pynixd/nix/functional-tests/README.md` for the test mode, and
              # issue #172 for the work.
              nixFunctionalTests = lib.mapAttrs (
                versionName: nixPackage:
                final.callPackage ./pynixd/nix/functional-tests/package.nix {
                  nix = nixPackage;
                  version = versionName;
                  inherit (final) pynixd;
                }
              ) supportedNixPackages;
              # What the suite needs on PATH, shared by the packaged runner
              # and the dev shell so the two cannot drift again. The file
              # says which drifts it already cost.
              suiteRuntime = final.callPackage ./nix/suite-runtime.nix {
                inherit tofuCoreSchemaTool storeExecTools;
              };
              shell = final.callPackage ./nix/shell.nix {
                pythonSet = final.editablePythonSet;
              };
              nonEditableShell = final.callPackage ./nix/shell.nix {
                inherit (final) pythonSet;
              };
              # A live, editable-install `pynix`/`ekn` env (no devtools --
              # see nix/shell.nix for the full interactive nanopynix shell),
              # exported so other repos can drop a hot-reloading `pynix`
              # into their own devShell/direnv without rebuilding on every
              # edit here. See nix/virtual-env.nix's own docstring for why no
              # env var is needed.
              pynixDevEnv = final.callPackage ./nix/virtual-env.nix {
                pythonSet = final.editablePythonSet;
              };
              pynixNonEditableDevEnv = final.callPackage ./nix/virtual-env.nix {
                inherit (final) pythonSet;
              };
              nanopynix-docs = final.callPackage ./nix/docs.nix { };
              # An attrset of derivations, not one derivation, so a failing
              # run names the gate. `flake.nix` puts it under `checks`; the
              # `packages` filter drops it, which is what we want.
              checks = final.callPackage ./nix/checks.nix { inherit completionSpike; };
            }
          ) scope.packages
        );

      # nix-store's sqlite buildInput + every meson-based nix-* library get
      # consistent instrumentation (see nix/sanitizer.nix) --
      # applied after extendNixScope (rather than on the raw nixComponents_X
      # scope) since overrideScope/overrideAllMesonComponents both survive
      # onto the extended scope, so nanopynix-bindings/nanopynix end up built
      # against the *same* instrumented nix-store/nix-expr/etc via the shared
      # `final` fixpoint.
      applySanitizerOverrides =
        scope:
        (scope.overrideScope (
          _final: prev:
          let
            # One boost for the three components that take it. `sanitizeBoost`
            # in nix/sanitizer.nix gives the one-definition-rule reason that
            # makes "the same one" load-bearing rather than tidy.
            boost = sanitizer.sanitizeBoost pkgs.boost;
            ucontextBoost = lib.optionalAttrs sanitizer.needsUcontextBoost { inherit boost; };
          in
          {
            nix-util = prev.nix-util.override ucontextBoost;
            nix-store = prev.nix-store.override (
              { sqlite = sanitizer.sanitizeSqlite pkgs.sqlite; } // ucontextBoost
            );
            # boost reaches nix-expr whatever the collector does, and boehmgc
            # does not. The comment below gives the reason boehmgc is absent
            # from a build with no collector, and that reason does not reach
            # boost: libexpr links boost in both builds.
            nix-expr = prev.nix-expr.override (ucontextBoost // boehmgcOverride);
          }
        )).overrideAllMesonComponents
          sanitizer.mesonComponentOverrides;

      # Absent from a build with no collector, and not merely unused there.
      # `enableGC = false` drops boehmgc from libexpr's inputs entirely, so
      # this would name a patched, instrumented library that nothing links --
      # one more thing for a reader to reconcile against a closure that does
      # not contain it.
      # pkgs.nixDependencies.boehmgc, not prev.boehmgc or pkgs.boehmgc:
      # nixComponents_X's own scope never contains a `boehmgc` attribute at
      # all -- nixpkgs builds each nixComponents_X via a *separate*
      # `nixDependencies` scope (`nixDependencies.callPackage
      # ./modular/packages.nix {...}` in nix/default.nix), and that
      # nixDependencies scope (packaging/dependencies.nix in nix's own source)
      # is where boehmgc's enableLargeConfig + 1MiB initial mark stack tuning
      # actually lives -- confirmed by `prev.boehmgc` failing eval with
      # "attribute 'boehmgc' missing". Sanitizing a fresh pkgs.boehmgc would
      # silently drop that tuning -- exactly the kind of undersized-mark-stack
      # condition its own comment warns about, right where we're chasing a GC
      # crash.
      boehmgcOverride = lib.optionalAttrs gc {
        boehmgc = sanitizer.sanitizeBoehmGC patchedBoehmGC;
      };

      # The patch, and nothing else. This runs before
      # `applySanitizerOverrides`, so a sanitized build still ends up with the
      # instrumented collector: both write `boehmgc`, and the later one wins.
      applyBoehmGCPatch =
        scope:
        scope.overrideScope (
          _final: prev: {
            nix-expr = prev.nix-expr.override { boehmgc = patchedBoehmGC; };
          }
        );

      # Drop the collector from libexpr, and from everything above it in the
      # scope. One override, applied at the same point and for the same reason
      # as `applySanitizerOverrides`: the scope fixpoint carries it to
      # nanopynix-bindings, so the extension links against the libexpr that
      # this scope built and not some other one.
      #
      # nixpkgs' own `libexpr/package.nix` turns `enableGC` into
      # `lib.mesonEnable "gc" enableGC` and drops `boehmgc` from
      # `propagatedBuildInputs`, so nothing else here has to know.
      applyNoGCOverrides =
        scope:
        scope.overrideScope (
          _final: prev: {
            nix-expr = prev.nix-expr.override { enableGC = false; };
          }
        );

      # nixComponents_2_34 -> nix_2_34, nixComponents_git -> git (matching
      # the names the previous hand-written patchedNixVersions used), plus a
      # suffix for each variant axis: "-tsan"/"-ubsan"/"-asan" for a sanitizer,
      # "-nogc" for a build with no collector.
      #
      # The ASAN variant takes "-asan" alone, although it is also a build with
      # no collector. `requiresNoGC` makes the two inseparable, so a
      # "-asan-nogc" name would repeat one fact twice and give CI a suffix that
      # two filters have to strip.
      rename =
        name: value:
        let
          bare = lib.removePrefix "nixComponents_" name;
          versionName = if bare == "git" then "git" else "nix_${bare}";
          suffix =
            if sanitizer != null then
              "-${sanitizer.suffix}"
            else if wheel then
              "-wheel"
            else if !gc then
              "-nogc"
            else
              "";
        in
        lib.nameValuePair "${versionName}${suffix}" value;
    in
    lib.pipe pkgs.nixVersions (
      [
        (lib.filterAttrs isNixScope)
        # **The supported floor, and every variant obeys it.** `git` passes
        # too: `majorMinor` of a `2.35pre...` version is `2.35`.
        (lib.filterAttrs (
          _: scope: lib.versionAtLeast (lib.versions.majorMinor scope.version) supportedNixFloor
        ))
      ]
      ++ [
        (lib.mapAttrs (_: patchNixScope))
        (lib.mapAttrs (_: extendNixScope))
      ]
      # Before the sanitizer, so an instrumented build still gets the
      # instrumented collector rather than this plain patched one.
      ++ lib.optional gc (lib.mapAttrs (_: applyBoehmGCPatch))
      ++ lib.optional (sanitizer != null) (lib.mapAttrs (_: applySanitizerOverrides))
      # After the sanitizer, so this is the last word on nix-expr. The two
      # overrides do not collide -- `applySanitizerOverrides` no longer names
      # nix-expr when `gc` is false -- and the order still says which one wins
      # if a third ever arrives.
      ++ lib.optional (!gc) (lib.mapAttrs (_: applyNoGCOverrides))
      # Last, so this is the final word on the stdenv of the scope. It writes
      # `nix-expr` too, and `applyBoehmGCPatch` above writes the same
      # attribute: the later one wins, and the collector that it names is the
      # patched one rebuilt for the wheel, so the patch survives the order.
      ++ lib.optional wheel (lib.mapAttrs (_: wheelNix.applyWheelOverrides))
      ++ [ (lib.mapAttrs' rename) ]
    );

  # Five variants of every supported Nix version. Nix evaluates each one
  # lazily, so the cost here is evaluation and not a build: CI names the
  # `nanopynix-tests-<variant>` package it wants, and nothing else realises.
  nanopynixVersionsInternal =
    nanopynixForNixVersions { }
    // nanopynixForNixVersions { sanitizer = sanitizers.tsan; }
    // nanopynixForNixVersions { sanitizer = sanitizers.ubsan; }
    # The collector build and the ASAN build, both of which run against a
    # libexpr with `-Dgc=disabled`. The plain one proves that the evaluator
    # works without the collector; the ASAN one is what that build exists for.
    # Separating them keeps a failure attributable: an ASAN job that goes red
    # while `-nogc` stays green is a memory error, and both red together is a
    # build without a collector that does not work.
    // nanopynixForNixVersions { gc = false; }
    // nanopynixForNixVersions {
      sanitizer = sanitizers.asan;
      gc = false;
    };

  nanopynixVersions = nanopynixVersionsInternal // {
    stable = getByVersion pkgs.nixVersions.stable.version;
    latest = getByVersion pkgs.nixVersions.latest.version;
  };

  # The wheel build, and **deliberately not a member of
  # `nanopynixVersionsInternal`.**
  #
  # Every CI job comes from that set: `tests` maps it to one
  # `nanopynix-tests-<name>` package for each entry, and `ciVersionMatrix`
  # groups the same names into the matrices. Adding "-wheel" there would put a
  # from-source rebuild of the whole C and C++ closure into the per-commit
  # matrix, on every version. That closure leaves the binary cache by
  # construction, because lowering the glibc floor is what it is for.
  #
  # So it lives here, reachable by name for a person who wants a wheel, and
  # invisible to the matrices. `variantSuffixes` needs no "-wheel" entry for the
  # same reason: `unlistedVariants` reads `nanopynixVersionsInternal`, and this
  # is not in it.
  #
  # One version only. A wheel carries one Nix, which is the whole reason a
  # wheel removes the ABI matrix.
  nanopynixForWheel = (nanopynixForNixVersions { wheel = true; }).nix_2_34-wheel;

  # The wheel itself. `nix/wheel.nix` runs `auditwheel repair` over the
  # extension above, which bundles each library and writes the `manylinux` tag.
  # Off the matrices for the same reason as the build it reads.
  # The licence text of every library that the wheel bundles. The package set
  # is the whole rebuilt closure plus the collector and the five Nix components,
  # which is every library that can end up in `nanopynix_bindings.libs/`.
  nanopynixWheelLicenses = pkgs.callPackage ./nix/wheel-licenses.nix { } {
    packages = wheelNix.wheelLibs // {
      boehmgc = wheelNix.wheelBoehmGC;
      # The one C++ runtime of the closure. Every C++ object of the wheel names
      # it, so the wheel carries it and the notice has to describe it.
      nanopynix-cxx-runtime = wheelNix.cxxRuntime;
      inherit (nanopynixForWheel)
        nix-util
        nix-store
        nix-expr
        nix-fetchers
        nix-flake
        ;
    };
  };

  nanopynixWheel = pkgs.callPackage ./nix/wheel.nix {
    inherit nanopython;
    inherit (nanoPythonPackages) auditwheel wheel;
    licenses = nanopynixWheelLicenses;
    inherit (wheelNix) cxxRuntime;
    inherit (wheelNix.cxxStdenv) lowerGlibc;
    bindings = nanopynixForWheel.nanopynix-bindings.override {
      # **The Nix version is in the name, and not in the version.**
      #
      # PyPI holds one name for one project, and this project builds one
      # artifact for each Nix version. Those artifacts are alternatives: each
      # imports as `nanopynix_bindings`, so two of them cannot be installed
      # together, and the name is what says so. `opencv-python` against
      # `opencv-python-headless` is the same shape.
      #
      # The version then stays the version of this project, which is what a
      # dependency specifier wants to name. The other route, a version of
      # `2.34.8.1` with the Nix version leading, reads well for a pin and
      # leaves this package no way to state a version of its own API.
      #
      # Major and minor only. A Nix patch release does not change the ABI that
      # the extension links, so `nix2-34` covers 2.34.8 and 2.34.9, and the
      # exact version stays in `build_info()` and in the metadata.
      pypiName = "nanopynix-bindings-nix${
        lib.replaceStrings [ "." ] [ "-" ] (lib.versions.majorMinor nanopynixForWheel.version)
      }";

      # **Here, and in no other build.** One `cp313-abi3` wheel imports on
      # every CPython from 3.13 up, so a release of CPython costs no rebuild.
      # Without it PyPI needs one wheel for each Python minor version, times
      # three Nix versions and two architectures.
      #
      # `nanopynix-bindings/package.nix` says why nothing that Nix builds wants
      # this: such a build serves one interpreter, and the stable ABI stops
      # nanobind reading the internals of CPython directly.
      stableAbi = true;
    };
  };

  # Per-version test runners, exposed individually as `nanopynix-tests-<name>`
  # flake packages so CI can build/run each Nix version in its own job.
  tests = lib.mapAttrs' (
    name: value: lib.nameValuePair "nanopynix-tests-${name}" value.nanopynix.test
  ) nanopynixVersionsInternal;

  # Every suffix that `rename` above gives a variant scope.
  #
  # **A suffix that is missing here does not fail.** It quietly puts a slow,
  # uncovered build into the regular per-commit matrix, because "not a variant"
  # is the default everywhere. `unlistedVariants` below turns that silence into
  # a build failure.
  variantSuffixes = [
    "-tsan"
    "-ubsan"
    "-asan"
    "-nogc"
  ];

  # The check that makes a forgotten suffix a build failure.
  #
  # `rename` above writes a suffix for each variant axis, and every consumer of
  # these names sorts by that suffix. A new axis that nobody adds to
  # `ci/variants.nix` reads as a regular version everywhere, so it joins the
  # per-commit matrix as a slow build that collects no coverage. Nothing else
  # notices, because "not a variant" is the default.
  unlistedVariants = builtins.filter (
    name: builtins.match "^(nix_[0-9_]+|git)$" name == null && !hasKnownSuffix name
  ) (builtins.attrNames nanopynixVersionsInternal);
  hasKnownSuffix = name: lib.any (suffix: lib.hasSuffix suffix name) variantSuffixes;

  # The version names of `tests`, grouped by variant, with the bare names under
  # `regular`. `ci/workflows/lib.nix` builds one job per entry, and
  # `ci/steps.nix` embeds the whole thing so that the scheduled workflow can
  # write its matrices with one `echo` rather than five `nix eval` calls.
  #
  # **Do not drop a version from a group to save CI minutes.** The settings and
  # store models carry 32 `nix_version_min`/`nix_version_removed` fields, and
  # the drift check is what proves each gate is set correctly: a field the
  # running Nix does not have shows up as `extra`, and a gate that hides a
  # field the running Nix does have shows up as `missing`. Neither can be seen
  # from one version. Dropping a version deletes that coverage in silence,
  # because the remaining jobs stay green.
  #
  # **Issue #126 dropped 2.31 anyway, and this is what it cost.** The gate
  # refused 31 of the 32 fields on 2.31, 15 on 2.34 and 1 on 2.35, so 2.31 was
  # the only job that saw a `since("2.34")` field refused. Those gates are now
  # true on every supported version, so the drift check never exercises them.
  # They are not dead -- a consumer can link an older Nix by hand -- but
  # nothing here proves them any more.
  #
  # That was a considered trade, and the measurement that bought it is in
  # `supportedNixFloor` above. The rule still holds for 2.34: it is the only
  # version that refuses a `since("2.35")` field, and dropping it would repeat
  # the loss with nothing left to catch it.
  ciVersionMatrix =
    let
      names = map (lib.removePrefix "nanopynix-tests-") (builtins.attrNames tests);
    in
    {
      regular = builtins.filter (name: !hasKnownSuffix name) names;
    }
    // builtins.listToAttrs (
      map (suffix: {
        name = lib.removePrefix "-" suffix;
        value = builtins.filter (lib.hasSuffix suffix) names;
      }) variantSuffixes
    );

  # The CI experiments. `ci/experiments.nix` gives the reason each one is a
  # package rather than a script in a workflow file.
  experiments = import ./ci/experiments.nix { inherit pkgs tests; };

  # Every body that a GitHub Actions step used to carry inline. `ci/steps.nix`
  # gives the reason each one is a package.
  ciSteps = import ./ci/steps.nix {
    inherit
      pkgs
      tests
      ciVersionMatrix
      variantSuffixes
      ;
  };

  getByVersion =
    version:
    lib.pipe nanopynixVersionsInternal [
      lib.attrsToList
      (lib.map (v: v.value))
      (lib.filter (v: v.version == version))
      (lib.head)
    ];
in
lib.throwIf (unlistedVariants != [ ])
  ''
    default.nix: these variant scopes carry a suffix that ci/variants.nix does
    not list, so every consumer reads them as regular Nix versions and CI runs
    them in the per-commit matrix with no coverage:
      ${builtins.concatStringsSep "\n    " unlistedVariants}
    Add the suffix to ci/variants.nix.
  ''
  {
    inherit (pkgs) lib;

    inherit (nanopynixVersions.stable)
      nanopynix
      nanopynix-bindings
      nanopynix-helpers
      nanopynix-proto
      # The command-line layer of issue #222. It reaches
      # `nanopynix-bindings` through nothing, so this one attribute is the
      # same package for every Nix version -- see `nixLinked` in
      # `nix/py-packages.nix`. It is under `stable` for consistency with its
      # neighbours here, and not because the version means anything to it.
      libpynix
      # `nanopynix.pytest-agent` is the *package*, built in this repo's
      # `pythonSet`. A consumer that assembles its own venv should not add it
      # from here -- mixing a package built in one builders set into another
      # set's venv does not resolve. Name it in that venv's own spec instead
      # (`pytest-agent = [ ];`), which works because `pythonSetWith` composes
      # this repo's project overlay into the consumer's set.
      pytest-agent
      pynix
      # The language server of `pynix-lsp/`, which issue #107 split out of
      # `pynix`. It is a second application, and not a variant of the first
      # one: an editor names one command, and `pynix-lsp` is the name that
      # every other Nix language server uses.
      pynix-lsp
      # The daemon proxy of `pynixd/`. `nixosModules.pynixd` in `flake.nix`
      # defaults `services.pynixd.package` to this one.
      pynixd
      # One runner of Nix's functional test suite for each supported Nix
      # version. An attrset of packages, so a person names the version:
      # `nix build --file . nixFunctionalTests.nix_2_34`.
      nixFunctionalTests
      pynixDevEnv
      pynixNonEditableDevEnv
      shell
      nonEditableShell
      nanopynix-docs
      checks
      # For a consumer that builds one of *its own* projects against this
      # repo's Python closure. easykubenix owns `ekn`'s source and renders it
      # on its own side, using `ps` and `mkApp` below.
      #
      # `pythonSetWith` is the one to reach for when that project has a
      # dependency this repo does not declare -- it seeds the set from the
      # consumer's pyproject.toml as well as ours, which is the only point at
      # which a nixpkgs package can enter. `pythonSet` is `pythonSetWith { }`,
      # for consumers that just want the closure as it stands.
      pythonSet
      pythonSetWith
      ;

    inherit
      flake
      pkgs
      nanopython
      nanoPythonPackages
      nanopynixVersions
      nanopynixForWheel
      # The C and C++ closure that the wheel bundles, and the stdenv that
      # builds it. `cxxRuntime` is the one C++ runtime of that closure, and it
      # is a build of its own that has its own gate.
      wheelNix
      nanopynixWheel
      nanopynixWheelLicenses
      pyproject-nix
      tests
      experiments
      ciSteps
      ciVersionMatrix
      tofuCoreSchemaTool
      storeExecTool
      storeExecTools
      # The seam onto pyproject.nix's builders, exported for the same reason
      # `pythonSet` above is. `ps.mkProject` renders a project from its own
      # pyproject.toml; `mkApp` turns one of those into a release application.
      #
      # `mkApp` is the part a consumer cannot do without. Its `caBundle`
      # wrapper is the fix for issue #62: a program that initialises OpenSSL
      # at import (anything pulling in pygit2) cannot start inside a Nix build
      # sandbox, which has no trust store. easykubenix runs its `ekn` CLI in
      # exactly that position from three derivations, and gates it on its own
      # side. See nix/mk-app.nix.
      ps
      mkApp
      ;
  }
