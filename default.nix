let
  flake = (import ./nix/compat.nix);
in
{
  inputs ? flake.inputs,
  system ? builtins.currentSystem,
  pkgs ? inputs.nixpkgs.legacyPackages.${system},
}:
let
  inherit (pkgs) lib python3Packages;

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

  inherit (pkgs.callPackage inputs.grpclib-transports { })
    grpclib-transports
    betterproto2
    betterproto2-compiler
    ;

  nanopynix-proto = python3Packages.callPackage ./nanopynix-proto/package.nix {
    inherit betterproto2 betterproto2-compiler renderPyproject;
  };

  clypi = python3Packages.callPackage ./nix/clypi.nix { };

  # Neither depends on any Nix-version-specific code (no nix-store/nix-expr/
  # nanopynix/nanopynix-bindings), so -- like clypi above -- these live here
  # rather than inside nanopynixForNixVersions/extendNixScope, built once
  # instead of once per Nix version. Only merged into callNixPythonPackage's
  # environment below (see `inherit kr8s tree-sitter-nix` in the merge) so
  # in-scope package.nix files (ekn, pynix, nanopynix-helpers) can still
  # resolve them by name.
  kr8s = python3Packages.callPackage ./nix/kr8s.nix { };

  # Not packaged in nixpkgs (checked 2026-07-27). Test-only (nanopynix's
  # `test` extra), so -- like clypi/kr8s above -- built once here and merged
  # into callNixPythonPackage's environment below rather than per Nix
  # version.
  pytest-beartype = python3Packages.callPackage ./nix/pytest-beartype.nix { };

  tree-sitter-nix = python3Packages.callPackage ./nix/tree-sitter-nix.nix {
    # `pkgs.path` (the nixpkgs source tree) would otherwise be shadowed by
    # python3Packages' own PyPI package literally named "path" if resolved
    # through python3Packages -- calling this directly with `pkgs.path`
    # sidesteps that entirely.
    nixpkgsPath = pkgs.path;
    # Same shadowing problem: python3Packages.tree-sitter is the PyPI
    # `tree-sitter` bindings package, not pkgs.tree-sitter (the CLI
    # derivation, whose passthru has `buildGrammar`).
    treeSitterCli = pkgs.tree-sitter;
    treeSitterNixSrc = inputs.tree-sitter-nix-numtide;
  };

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
      # from python3Packages (which has its own, incompatible `brotli`: the
      # Python bindings, not the C library with a pkg-config .pc file). Our
      # own packages instead go through callNixPythonPackage, a second
      # callPackage-like function that also has python3Packages/pkgs/our
      # extras in scope (mirroring the pre-carlmazing nanopynixForNixEx's
      # `pkgs // python3Packages // {...} // extra`), plus `final` so they can
      # still reference nix-store/nix-expr/nanopynix-bindings/etc directly.
      extendNixScope =
        scope:
        lib.makeScope scope.newScope (
          lib.extends (
            final: _prev:
            let
              tsanRuntime = if enableTsan then tsan.tsanRuntime else null;
              callNixPythonPackage = lib.callPackageWith (
                pkgs
                // python3Packages
                // {
                  inherit
                    grpclib-transports
                    renderPyproject
                    renderEditablePyproject
                    betterproto2
                    betterproto2-compiler
                    nanopynix-proto
                    clypi
                    kr8s
                    pytest-beartype
                    tree-sitter-nix
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
