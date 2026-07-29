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
  # Strictly additive -- every name is this repo's own or vendored under
  # nix/, never an override of an existing nixpkgs attribute -- so it forces
  # no rebuild of nixpkgs' own Python packages and leaves the interpreter
  # derivation itself untouched.
  pythonBase = pkgs.python3.override {
    packageOverrides = pySelf: _pyPrev: {
      # Built by their own repo against plain `pkgs.python3Packages`. Same
      # interpreter, and we only add names, so there is no second instance
      # of anything to collide.
      inherit (pkgs.callPackage inputs.grpclib-transports { })
        grpclib-transports
        betterproto2
        betterproto2-compiler
        ;

      clypi = pySelf.callPackage ./nix/clypi.nix { };

      kr8s = pySelf.callPackage ./nix/kr8s.nix { };

      tree-sitter-nix = pySelf.callPackage ./nix/tree-sitter-nix.nix {
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

  tsan = pkgs.callPackage ./nix/tsan.nix { };

  # A confirmed data race in nix::Bindings::emptyBindings (a process-wide
  # shared static that ExprAttrs::eval unconditionally writes to -- see the
  # patch's own commentary) found via ThreadSanitizer (see enableTsan below).
  # thread_local gives each evaluator OS thread its own instance, fixing the
  # race without any behavior change for single-threaded use.
  emptyBindingsPatch = ./nix/patches/nix-thread-local-empty-bindings.patch;

  # printValueAsJSON recurses without consulting max-call-depth on 2.31, so a
  # cyclic value with no `outPath`/`__toString` to stop at overflows the C++
  # stack and SIGSEGVs the process instead of raising -- and nanopynix reaches
  # that function from `Value.to_python()`. Upstream's own one-line fix,
  # already present from 2.34 on; see the patch header for the provenance.
  valueToJsonCallDepthPatch = ./nix/patches/nix-2.31-value-to-json-call-depth.patch;

  # Which patches to apply to a given nix version's modular component set,
  # keyed by that version's own major.minor (e.g. "2.34"), with `default`
  # as the fallback for anything without its own entry (git's rolling
  # pre-release version string, any future point release, ...).
  nixPatches = {
    default = [ emptyBindingsPatch ];
    "2.34" = [ emptyBindingsPatch ];
    "2.35" = [ emptyBindingsPatch ];
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
    "2.31" = [ valueToJsonCallDepthPatch ];
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
    { enableTsan }:
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
              tsanRuntime = if enableTsan then tsan.tsanRuntime else null;

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
                inherit enableTsan tsanRuntime;
              };

              # Everything above the bindings is a pyproject.nix builders
              # package. The set is built once per Nix version and holds both
              # the built and the editable form of each project.
              # No enableTsan/tsanRuntime: nothing in these pure-Python
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
                  inherit tsanRuntime;
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
      # consistent -fsanitize=thread instrumentation (see nix/tsan.nix) --
      # applied after extendNixScope (rather than on the raw nixComponents_X
      # scope) since overrideScope/overrideAllMesonComponents both survive
      # onto the extended scope, so nanopynix-bindings/nanopynix end up built
      # against the *same* instrumented nix-store/nix-expr/etc via the shared
      # `final` fixpoint.
      applyTsanOverrides =
        scope:
        (scope.overrideScope (
          _final: prev: {
            nix-store = prev.nix-store.override { sqlite = tsan.sanitizeSqlite pkgs.sqlite; };
            # pkgs.nixDependencies.boehmgc, not prev.boehmgc or pkgs.boehmgc:
            # nixComponents_X's own scope never contains a `boehmgc` attribute
            # at all -- nixpkgs builds each nixComponents_X via a *separate*
            # `nixDependencies` scope (`nixDependencies.callPackage
            # ./modular/packages.nix {...}` in nix/default.nix), and that
            # nixDependencies scope (packaging/dependencies.nix in nix's own
            # source) is where boehmgc's enableLargeConfig + 1MiB initial
            # mark stack tuning actually lives -- confirmed by `prev.boehmgc`
            # failing eval with "attribute 'boehmgc' missing". Sanitizing a
            # fresh pkgs.boehmgc would silently drop that tuning -- exactly
            # the kind of undersized-mark-stack condition its own comment
            # warns about, right where we're chasing a GC crash.
            nix-expr = prev.nix-expr.override {
              boehmgc = tsan.sanitizeBoehmGC pkgs.nixDependencies.boehmgc;
            };
          }
        )).overrideAllMesonComponents
          tsan.mesonComponentOverrides;

      # nixComponents_2_34 -> nix_2_34, nixComponents_git -> git (matching
      # the names the previous hand-written patchedNixVersions used), plus a
      # "-tsan" suffix for the ThreadSanitizer variant.
      rename =
        name: value:
        let
          bare = lib.removePrefix "nixComponents_" name;
          versionName = if bare == "git" then "git" else "nix_${bare}";
          suffix = if enableTsan then "-tsan" else "";
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
      ++ lib.optional enableTsan (
        lib.filterAttrs (_: scope: lib.versions.majorMinor scope.version != "2.31")
      )
      ++ [
        (lib.mapAttrs (_: patchNixScope))
        (lib.mapAttrs (_: extendNixScope))
      ]
      ++ lib.optional enableTsan (lib.mapAttrs (_: applyTsanOverrides))
      ++ [ (lib.mapAttrs' rename) ]
    );

  nanopynixVersionsInternal =
    nanopynixForNixVersions { enableTsan = false; } // nanopynixForNixVersions { enableTsan = true; };

  nanopynixVersions = nanopynixVersionsInternal // {
    stable = getByVersion pkgs.nixVersions.stable.version;
    latest = getByVersion pkgs.nixVersions.latest.version;
  };

  # Per-version test runners, exposed individually as `nanopynix-tests-<name>`
  # flake packages so CI can build/run each Nix version in its own job.
  tests = lib.mapAttrs' (
    name: value: lib.nameValuePair "nanopynix-tests-${name}" value.nanopynix.test
  ) nanopynixVersionsInternal;

  getByVersion =
    version:
    lib.pipe nanopynixVersionsInternal [
      lib.attrsToList
      (lib.map (v: v.value))
      (lib.filter (v: v.version == version))
      (lib.head)
    ];
in
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
    pyproject-nix
    tests
    tofuCoreSchemaTool
    storeExecTool
    ;
}
