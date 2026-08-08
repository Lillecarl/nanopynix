let
  flake = (import ./nix/compat.nix);
in
{
  inputs ? flake.inputs,
  system ? builtins.currentSystem,
  pkgs ? inputs.nixpkgs.legacyPackages.${system},
}:
let
  inherit (pkgs) lib;

  pyproject-nix = import "${inputs.pyproject-nix}" { inherit lib; };

  # Every Python package this repo needs that does *not* depend on
  # nanopynix-bindings, added to the interpreter's own package set.
  #
  # The division is the point. Nothing here reaches nix-store/nix-expr or the
  # bindings, so none of it varies by Nix version and all of it is built once
  # rather than once per version. Putting it in the base interpreter rather
  # than in the per-version scope makes that structural instead of a
  # convention someone has to keep: the per-version overlay further down can
  # only usefully add packages that need the bindings, because everything
  # else already resolves before it runs. Adding a version-independent
  # package to that overlay by mistake would rebuild it three times over,
  # and there would be nothing to notice it.
  #
  # `pySelf.callPackage`, not `python3Packages.callPackage`: these must be
  # members of the set that resolves their dependencies, or `clypi` built
  # against the plain set and `clypi` seen from this one would be two
  # derivations of one source.
  #
  # Additive, with one exception that says why it is here and when it goes.
  # Every other name is this repo's own or vendored under nix/, so this set
  # forces no rebuild of nixpkgs' own Python packages and leaves the
  # interpreter derivation itself untouched.
  pythonBase = pkgs.python3.override {
    packageOverrides = pySelf: pyPrev: {
      # THE ONE OVERRIDE. Delete it when nixpkgs PR #548078 reaches the
      # nixpkgs this repo pins.
      #
      # datamodel-code-generator runs ruff over the code it generates and
      # compares the result against a checked-in expectation. A newer ruff
      # writes a blank line after `from __future__ import annotations`, so
      # five of its tests fail, and nixpkgs ships the package with a failing
      # test suite. It reaches this repo through tree-sitter-config, which
      # nix/tree-sitter-nix.nix needs, so the failure took out the whole
      # scheduled build: 18 of 19 jobs, three nights running.
      #
      # The list is upstream's list, and not the two failures that were
      # visible. nixpkgs passes --maxfail=2, so the run stopped at the second
      # one and the other three were never reported.
      datamodel-code-generator = pyPrev.datamodel-code-generator.overridePythonAttrs (old: {
        disabledTests = (old.disabledTests or [ ]) ++ [
          "test_no_use_type_checking_imports"
          "test_ruff_batch_formatting_directory"
          "test_ruff_check_and_format_combined"
          "test_ruff_check_only"
          "test_type_checking_imports_default_to_runtime_imports_for_modular_pydantic_ruff"
        ];
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
      betterproto2 = pySelf.callPackage ./nix/betterproto2.nix { };
      betterproto2-compiler = pySelf.callPackage ./nix/betterproto2-compiler.nix { };

      clypi = pySelf.callPackage ./nix/clypi.nix { };

      kr8s = pySelf.callPackage ./nix/kr8s.nix { };

      tree-sitter-nix = pySelf.callPackage ./nix/tree-sitter-nix.nix {
        # This set, and not `python.pkgs`, which is the set from before
        # these overrides ran. nix/tree-sitter-nix.nix gives the
        # measurement.
        pythonPackages = pySelf;
        # `pkgs.path` (the nixpkgs source tree) would otherwise be shadowed
        # by the Python set's own PyPI package literally named "path" --
        # passing `pkgs.path` explicitly sidesteps that entirely.
        nixpkgsPath = pkgs.path;
        # Same shadowing problem: the set's `tree-sitter` is the PyPI
        # bindings package, not pkgs.tree-sitter (the CLI derivation, whose
        # passthru has `buildGrammar`).
        treeSitterCli = pkgs.tree-sitter;
        treeSitterNixSrc = inputs.tree-sitter-nix-numtide;
      };
    };
  };

  # Exports OpenTofu's built-in ("core") HCL block schema
  # (resource/data/count/for_each/lifecycle/...) as JSON for a given OpenTofu
  # version, on demand -- see tools/tofu-core-schema/package.nix and
  # pynix/src/pynix/_lsp/_tofu_core_schema.py, which invokes this at LSP-
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
  # The TSAN matrix skips 2.31 (see `nanopynixForNixVersions` below), and 2.31
  # is the one version where the ownership rules that UBSan is here to check
  # differ. On its own it runs everywhere.
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

  # Every C and C++ library of the closure, from the zig stdenv. The file
  # gives the package list, the payload trim and the corrections that each
  # package needs.
  #
  # **At the top level, and not in the `let` of `nanopynixForNixVersions`.**
  # `nanopynixWheel` reads `zigLibs` to name the licence of each library that
  # rides in the wheel, and that binding is a sibling of this one. Neither this
  # import nor `patchedBoehmGC` above reads an argument of that function, so
  # both moved out whole.
  zigNix = import ./nix/zig-nix.nix {
    inherit lib pkgs;
    boehmgc = patchedBoehmGC;
    python = pythonBase;
  };

  # A confirmed data race in nix::Bindings::emptyBindings (a process-wide
  # shared static that ExprAttrs::eval unconditionally writes to -- see the
  # patch's own commentary) found via ThreadSanitizer (see `sanitizers` above).
  # thread_local gives each evaluator OS thread its own instance, fixing the
  # race without any behavior change for single-threaded use.
  emptyBindingsPatch = ./nix/patches/nix-thread-local-empty-bindings.patch;

  # printValueAsJSON recurses without consulting max-call-depth on 2.31, so a
  # cyclic value with no `outPath`/`__toString` to stop at overflows the C++
  # stack and SIGSEGVs the process instead of raising -- and nanopynix reaches
  # that function from `Value.to_python()`. Upstream's own one-line fix,
  # already present from 2.34 on; see the patch header for the provenance.
  valueToJsonCallDepthPatch = ./nix/patches/nix-2.31-value-to-json-call-depth.patch;

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
  # Two files for one change, because `describe` is byte-identical in 2.31,
  # 2.34, 2.35 and git, and `emitTreeAttrs` is not: 2.31 writes the call on
  # one line and takes no `state.mem`. The hunks cannot be shared, so each
  # file carries both of its own.
  gmtimePatch = ./nix/patches/nix-gmtime-not-thread-safe.patch;
  gmtimePatch231 = ./nix/patches/nix-2.31-gmtime-not-thread-safe.patch;

  # Which patches to apply to a given nix version's modular component set,
  # keyed by that version's own major.minor (e.g. "2.34"), with `default`
  # as the fallback for anything without its own entry (git's rolling
  # pre-release version string, any future point release, ...).
  nixPatches = {
    default = [
      emptyBindingsPatch
      baseEnvSizePatch
      gmtimePatch
    ];
    "2.34" = [
      emptyBindingsPatch
      baseEnvSizePatch
      gmtimePatch
    ];
    "2.35" = [
      emptyBindingsPatch
      baseEnvSizePatch
      gmtimePatch
    ];
    # 2.31 is the one version that gets neither the same list nor the
    # default. emptyBindingsPatch is absent because 2.31's surrounding
    # attr-set.cc/.hh source doesn't match its hunks (confirmed by trying),
    # and 2.31 is the lowest-priority supported version, so it goes without
    # that patch rather than with a broken one -- revisit if/when 2.31
    # support is reconsidered.
    #
    # valueToJsonCallDepthPatch is 2.31-only in the other direction: 2.34+
    # already carry it upstream, and applying it there would fail on the
    # context it's trying to add.
    #
    # baseEnvSizePatch applies to every version. The three hunks have identical
    # context in 2.31, 2.34 and 2.35, so only the line numbers move.
    #
    # gmtimePatch231 is the 2.31 form of the same repair that gmtimePatch
    # makes everywhere else. Only its `emitTreeAttrs` hunk differs.
    "2.31" = [
      valueToJsonCallDepthPatch
      baseEnvSizePatch
      gmtimePatch231
    ];
  };

  patchesFor = scope: nixPatches.${lib.versions.majorMinor scope.version} or nixPatches.default;

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
      # Whether the whole C and C++ closure comes from the zig stdenv, which
      # targets an old glibc so that a PyPI wheel runs off NixOS.
      #
      # A whole scope, and for the same reason as the sanitizer above: a wheel
      # takes the highest glibc floor of everything that it carries, so one
      # library of the stdenv of nixpkgs holds the whole wheel at `GLIBC_2.38`.
      # `nix/zig-nix.nix` names each package, and issue #111 holds the
      # measurements.
      zig ? false,
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

              pythonSet = ps.mkPythonSet {
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
                  inherit (final.pyPackages) projectRoots;
                  # A nixpkgs Python package, but this scope's own -- lifted
                  # in as a root above rather than looked up by name.
                  exclude = [ "nanopynix-bindings" ];
                };
                overlay = final.pyPackages.built;
              };

              # The same set with our projects swapped for editable installs.
              # `mkVirtualEnv` from here gives a venv whose site-packages
              # points back at this checkout.
              editablePythonSet = final.pythonSet.overrideScope final.pyPackages.editable;

              inherit (final.pythonSet)
                nanopynix-proto
                nanopynix-helpers
                ;

              nanopynix = final.pythonSet.nanopynix // {
                test = final.callPackage ./nanopynix/tests.nix {
                  inherit (final.nanopynix) version;
                  inherit (inputs) nixpkgs;
                  inherit sanitizer sanitizerRuntime;
                  inherit (final) pythonSet;
                  # The same tools the `pynix` app above puts on its PATH, for
                  # the same reason -- the LSP tests drive the real handlers,
                  # so they need them exactly as the released program does.
                  inherit tofuCoreSchemaTool storeExecTool;
                };
              };

              # ekn's own Python source lives here (migrated from
              # easykubenix/ekn), so it is built against exactly the
              # nanopynix/nanopynix-bindings built in this same scope, with no
              # cross-repo source reference. easykubenix consumes this
              # build's output (`nanopynix.ekn`) for its own
              # parseYamlStream.nix IFD fallback rather than building its own.
              #
              # A release build is `mkApplication` over a venv: the venv has
              # the real dependency closure, and mkApplication symlinks out
              # just the parts of the package's own `$out` that belong in a
              # program, leaving site-packages behind.
              ekn = mkApp {
                name = "ekn";
                inherit (final) pythonSet;
                completions.var = "_EKN_COMPLETE";
                # `ekn` imports pygit2, which initialises OpenSSL at import
                # and refuses to start where there is no trust store. Three
                # easykubenix derivations run `ekn` inside a build sandbox,
                # which is exactly that. See issue #62. `pynix` needs no
                # bundle: it imports no OpenSSL consumer at start-up, and Nix
                # itself holds the certificates for the fetching it drives.
                caBundle = true;
              };

              pynix = mkApp {
                name = "pynix";
                inherit (final) pythonSet;
                # pynix._lsp._tofu_core_schema shells out to this at LSP
                # runtime rather than baking a static snapshot, so it has to
                # be on the program's PATH.
                #
                # storeExecTool likewise: nanopynix.store_exec_prefix resolves
                # it off PATH, and the terranix dialect execs `tofu` straight
                # out of whatever store the LSP is evaluating against -- which
                # is relocated whenever pynix is pointed at a non-root store.
                pathInputs = [
                  tofuCoreSchemaTool
                  storeExecTool
                ];
              };
              shell = final.callPackage ./nix/shell.nix { inherit tofuCoreSchemaTool storeExecTool; };
              # A live, editable-install `pynix`/`ekn` env (no devtools --
              # see nix/shell.nix for the full interactive nanopynix shell),
              # exported so other repos can drop a hot-reloading `pynix`
              # into their own devShell/direnv without rebuilding on every
              # edit here. See nix/dev-env.nix's own docstring for why no
              # env var is needed.
              pynixDevEnv = final.callPackage ./nix/dev-env.nix { };
              nanopynix-docs = final.callPackage ./nix/docs.nix { };
              # An attrset of derivations, not one derivation, so a failing
              # run names the gate. `flake.nix` puts it under `checks`; the
              # `packages` filter drops it, which is what we want.
              checks = final.callPackage ./nix/checks.nix { };
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
            else if zig then
              "-zig"
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
      ]
      # 2.31 predates the "make emptyBindings a global constant" refactor
      # (nix commit 4df1a3ca7, first in 2.32.0) and still carries its own
      # nrExprs++ as a plain unsigned long (fixed upstream by counter.hh's
      # atomic Counter type, also post-2.31) -- both are known, unfixed
      # races on this version alone, so TSAN just reports/aborts on
      # long-since-fixed bugs rather than anything actionable. Skip building
      # a TSAN variant for it entirely instead of chasing that noise.
      # TSAN only. UBSan keeps 2.31, and needs it: the ownership rules that
      # differ between versions -- `fetchers::Settings` living inside
      # `fetchers::Input` on 2.31 and not after -- are exactly what it is
      # there to check.
      ++ lib.optional (sanitizer != null && sanitizer.name == "thread") (
        lib.filterAttrs (_: scope: lib.versions.majorMinor scope.version != "2.31")
      )
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
      # patched one rebuilt with zig, so the patch survives the order.
      ++ lib.optional zig (lib.mapAttrs (_: zigNix.applyZigOverrides))
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
  # groups the same names into the matrices. Adding "-zig" there would put a
  # from-source rebuild of the whole C and C++ closure into the per-commit
  # matrix, on every version. That closure leaves the binary cache by
  # construction, because lowering the glibc floor is what it is for.
  #
  # So it lives here, reachable by name for a person who wants a wheel, and
  # invisible to the matrices. `variantSuffixes` needs no "-zig" entry for the
  # same reason: `unlistedVariants` reads `nanopynixVersionsInternal`, and this
  # is not in it.
  #
  # One version only. A wheel carries one Nix, which is the whole reason a
  # wheel removes the ABI matrix.
  nanopynixZig = (nanopynixForNixVersions { zig = true; }).nix_2_34-zig;

  # The wheel itself. `nix/wheel.nix` runs `auditwheel repair` over the
  # extension above, which bundles each library and writes the `manylinux` tag.
  # Off the matrices for the same reason as the build it reads.
  # The licence text of every library that the wheel bundles. The package set
  # is the whole zig closure plus the collector and the five Nix components,
  # which is every library that can end up in `nanopynix_bindings.libs/`.
  nanopynixWheelLicenses = pkgs.callPackage ./nix/wheel-licenses.nix { } {
    packages = zigNix.zigLibs // {
      boehmgc = zigNix.zigBoehmGC;
      # The one C++ runtime of the closure. Every C++ object of the wheel names
      # it, so the wheel carries it and the notice has to describe it.
      nanopynix-zig-cxx-runtime = zigNix.zigStdenv.cxxRuntime;
      inherit (nanopynixZig)
        nix-util
        nix-store
        nix-expr
        nix-fetchers
        nix-flake
        ;
    };
  };

  nanopynixWheel = pkgs.callPackage ./nix/wheel.nix {
    inherit (pkgs.python3Packages) auditwheel wheel;
    licenses = nanopynixWheelLicenses;
    bindings = nanopynixZig.nanopynix-bindings.override {
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
        lib.replaceStrings [ "." ] [ "-" ] (lib.versions.majorMinor nanopynixZig.version)
      }";
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
  # from one version. Measured: the gate refuses 31 of the 32 fields on 2.31,
  # 15 on 2.34 and 1 on 2.35, so each version reaches a different part of the
  # check. Dropping a version deletes that coverage in silence, because the
  # remaining jobs stay green.
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
      ekn
      pynix
      pynixDevEnv
      shell
      nanopynix-docs
      checks
      ;

    inherit
      flake
      pkgs
      nanopynixVersions
      nanopynixZig
      # The C and C++ closure that the wheel bundles, and the stdenv that
      # builds it. `zigStdenv.cxxRuntime` is the one C++ runtime of that
      # closure, and it is a build of its own that has its own gate.
      zigNix
      nanopynixWheel
      nanopynixWheelLicenses
      pyproject-nix
      tests
      experiments
      ciSteps
      ciVersionMatrix
      tofuCoreSchemaTool
      storeExecTool
      ;
  }
