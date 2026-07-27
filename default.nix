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

  renderPyproject =
    {
      projectRoot,
      python,
      pythonPackages ? python.pkgs,
    }:
    (pyproject-nix.lib.project.loadPyproject { inherit projectRoot; }).renderers.buildPythonPackage {
      inherit python pythonPackages;
    };

  renderEditablePyproject =
    {
      projectRoot,
      root,
      python,
      pythonPackages ? python.pkgs,
      extras ? [ ],
    }:
    (pyproject-nix.lib.project.loadPyproject { inherit projectRoot; }).renderers.mkPythonEditablePackage
      {
        inherit
          root
          python
          pythonPackages
          extras
          ;
      };

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

      # `python = pythonBase`, not the `python` that `callPackage` would
      # supply from the set. `self.python` is the *un-overridden*
      # interpreter -- `(python3.override { packageOverrides = ... })
      # .pkgs.python.pkgs` does not contain the overrides, which is easy to
      # miss and fails far away: `renderPyproject` defaults
      # `pythonPackages` to `python.pkgs`, so the renderer would resolve
      # `betterproto2` against plain nixpkgs and report it missing.
      # Self-reference is fine here -- `packageOverrides` is not forced
      # until the set is.
      nanopynix-proto = pySelf.callPackage ./nanopynix-proto/package.nix {
        inherit renderPyproject;
        python = pythonBase;
      };

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

  inherit (pythonBase.pkgs)
    grpclib-transports
    betterproto2
    betterproto2-compiler
    nanopynix-proto
    clypi
    kr8s
    tree-sitter-nix
    ;

  # Exports OpenTofu's built-in ("core") HCL block schema
  # (resource/data/count/for_each/lifecycle/...) as JSON for a given OpenTofu
  # version, on demand -- see tools/tofu-core-schema/package.nix and
  # pynix/src/pynix/_lsp/_tofu_core_schema.py, which invokes this at LSP-
  # server runtime rather than baking a static snapshot. Independent of any
  # nanopynix/Nix version, so it lives here rather than inside
  # nanopynixForNixVersions.
  tofuCoreSchemaTool = pkgs.callPackage ./tools/tofu-core-schema/package.nix { };

  tsan = pkgs.callPackage ./nix/tsan.nix { };

  # A confirmed data race in nix::Bindings::emptyBindings (a process-wide
  # shared static that ExprAttrs::eval unconditionally writes to -- see the
  # patch's own commentary) found via ThreadSanitizer (see enableTsan below).
  # thread_local gives each evaluator OS thread its own instance, fixing the
  # race without any behavior change for single-threaded use.
  emptyBindingsPatch = ./nix/patches/nix-thread-local-empty-bindings.patch;

  # Which patches to apply to a given nix version's modular component set,
  # keyed by that version's own major.minor (e.g. "2.34"), with `default`
  # as the fallback for anything without its own entry (git's rolling
  # pre-release version string, any future point release, ...).
  nixPatches = {
    default = [ emptyBindingsPatch ];
    "2.34" = [ emptyBindingsPatch ];
    "2.35" = [ emptyBindingsPatch ];
    # 2.31's surrounding attr-set.cc/.hh source doesn't match
    # emptyBindingsPatch's hunks (confirmed by trying), and it's the
    # lowest-priority supported version, so it's left unpatched rather
    # than broken -- revisit if/when 2.31 support is reconsidered.
    "2.31" = [ ];
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

              # `pythonBase` plus this repo's bindings-dependent packages --
              # and *only* those. Everything version-independent is already in
              # the base set, so this overlay is exactly the list of things
              # that genuinely differ per Nix version.
              #
              # Why a package set at all: so a dependency *name* resolves the
              # same way for pyproject.nix's renderers as for
              # `python.withPackages`. Before it, each package.nix rebuilt an
              # ad-hoc set inline (`pythonPackages = python.pkgs // { inherit
              # nanopynix clypi ...; }`), naming by hand the subset of local
              # packages that file happened to need. Nine such sets had
              # accumulated, and a name absent from one was not an error --
              # `getDependencies` only looks up what a pyproject.toml
              # declares, so an omission stayed invisible until some *other*
              # project declared that name. That is how pynix's `ekn` extra
              # could not resolve.
              #
              # `composeExtensions` rather than a plain `packageOverrides`:
              # `.override` replaces its argument, so overriding `pythonBase`
              # again with a bare `packageOverrides` would silently drop
              # clypi, kr8s, nanopynix-proto and the rest -- the failure would
              # surface far from here, as an unresolvable dependency name.
              #
              # Local packages come from `final` rather than being rebuilt, so
              # `python.pkgs.nanopynix` and the scope's own `nanopynix` are
              # one derivation. Laziness makes the knot fine: a package.nix
              # resolves its *dependencies* through this set, never itself.
              python = pythonBase.override (old: {
                packageOverrides = lib.composeExtensions (old.packageOverrides or (_: _: { })) (
                  _pySelf: _pyPrev: {
                    inherit (final)
                      nanopynix-bindings
                      nanopynix
                      nanopynix-helpers
                      ekn
                      pynix
                      ;
                  }
                );
              });

              # Only non-package arguments are listed here now. Every Python
              # dependency -- ours and nixpkgs' alike -- arrives through
              # `python.pkgs`, which is the whole point of having built one
              # set: there is no second place a package name can come from,
              # and so no way for the two to disagree.
              callNixPythonPackage = lib.callPackageWith (
                pkgs
                // python.pkgs
                // {
                  inherit
                    python
                    renderPyproject
                    renderEditablePyproject
                    pyproject-nix
                    tofuCoreSchemaTool
                    ;
                }
                // final
              );
            in
            {
              nanopynix-bindings = callNixPythonPackage ./nanopynix-bindings/package.nix {
                inherit enableTsan tsanRuntime;
              };
              nanopynix =
                callNixPythonPackage ./nanopynix/package.nix {
                  inherit enableTsan tsanRuntime;
                }
                // {
                  test = callNixPythonPackage ./nanopynix/tests.nix {
                    inherit (final.nanopynix) version;
                    inherit (inputs) nixpkgs;
                    inherit tsanRuntime;
                  };
                };
              nanopynix-helpers = callNixPythonPackage ./nanopynix-helpers/package.nix { };
              # ekn's own Python source now lives here (migrated from
              # easykubenix/ekn -- see ekn/package.nix), so it's built
              # against exactly the nanopynix/nanopynix-bindings wheels
              # built in this same scope, with no cross-repo source
              # reference. easykubenix consumes this build's output
              # (`nanopynix.ekn`) for its own parseYamlStream.nix IFD
              # fallback rather than building its own copy.
              ekn = callNixPythonPackage ./ekn/package.nix { };
              pynix = callNixPythonPackage ./pynix/package.nix {
                inherit (final) ekn;
              };
              shell = callNixPythonPackage ./nix/shell.nix { };
              # A live, editable-install `pynix`/`ekn` env (no devtools --
              # see nix/shell.nix for the full interactive nanopynix shell),
              # exported so other repos can drop a hot-reloading `pynix`
              # into their own devShell/direnv without rebuilding on every
              # edit here. See nix/dev-env.nix's own docstring for why no
              # env var is needed.
              pynixDevEnv = (callNixPythonPackage ./nix/dev-env.nix { }).pythonEnv;
              nanopynix-docs = callNixPythonPackage ./nix/docs.nix { };
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
    ekn
    pynix
    pynixDevEnv
    shell
    nanopynix-docs
    ;

  inherit
    flake
    pkgs
    nanopynixVersions
    nanopynix-proto
    clypi
    grpclib-transports
    pyproject-nix
    tests
    tofuCoreSchemaTool
    ;
}
