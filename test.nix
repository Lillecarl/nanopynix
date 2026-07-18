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

  nanopynix-proto = python3Packages.callPackage ./proto/package.nix {
    inherit betterproto2 betterproto2-compiler renderPyproject;
  };

  clypi = python3Packages.callPackage ./nix/clypi.nix { };

  tsan = pkgs.callPackage ./nix/tsan.nix { };

  # A confirmed data race in nix::Bindings::emptyBindings (a process-wide
  # shared static that ExprAttrs::eval unconditionally writes to -- see the
  # patch's own commentary) found via ThreadSanitizer (tsanNixPackage
  # below). thread_local gives each evaluator OS thread its own instance,
  # fixing the race without any behavior change for single-threaded use.
  emptyBindingsPatch = ./nix/patches/nix-thread-local-empty-bindings.patch;

  # Which patches to apply to a given nix version's modular component set,
  # keyed by that version's own major.minor (e.g. "2.34"), with `default`
  # as the fallback for anything without its own entry (git's rolling
  # pre-release version string, any future point release, ...). Single
  # source of truth for both patchNixComponents and the `-tsan` variants
  # below, instead of hardcoding the same per-version exception twice.
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

  patchesFor =
    components: nixPatches.${lib.versions.majorMinor components.nix-cli.version} or nixPatches.default;

  # Applies this version's patches (see nixPatches above) to a modular nix
  # component set (the ones exposing `.appendPatches`/`nix-everything`,
  # i.e. nixComponents_2_31 and newer) and re-merges it into a single
  # "nix" package, so it can be fed into nanopynixForNix exactly like an
  # unpatched nixVersions.* entry.
  patchNixComponents = components: (components.appendPatches (patchesFor components)).nix-everything;

  # Applies ThreadSanitizer instrumentation to a modular nix component set:
  # nix-store's sqlite input plus every meson-based nix-* library gets
  # consistent -fsanitize=thread instrumentation (see nix/tsan.nix), then
  # components are re-merged into one "nix" package via nix-everything so
  # the result can be fed into nanopynixForNix exactly like any other
  # nixVersions.* entry. Generalized from a nix_2_35-only build (that's how
  # the emptyBindings race below was originally found) to every version, so
  # a race can be confirmed/ruled out on any of them, not just the one it
  # happened to be found on.
  tsanNixComponents =
    components:
    (components.overrideScope (
      _final: prev: {
        nix-store = prev.nix-store.override { sqlite = tsan.sanitizeSqlite pkgs.sqlite; };
      }
    )).overrideAllMesonComponents
      tsan.mesonComponentOverrides;

  # nix-everything normally gate-checks the whole build on nix's own
  # unit/functional test suites (checkInputs pulls in nix-*-tests.tests.run
  # derivations regardless of doCheck, since they're still realized inputs).
  # Those suites weren't written with TSAN's overhead/timing changes in mind
  # and fail for reasons unrelated to the races being hunted here, so drop
  # the dependency on them entirely rather than debug nix's own test suite.
  tsanNixPackage =
    components:
    (tsanNixComponents components).nix-everything.overrideAttrs (_old: {
      checkInputs = [ ];
      doCheck = false;
    });

  # The real (non-TSAN) nixVersions entries actually exercised by the CI
  # matrix (see nix/dedupe-nix-versions.nix / nanopynixTestVersions below).
  # nix_2_31 is left as the plain, unpatched upstream entry (via the `//`
  # merge below) rather than rebuilt through patchNixComponents, since
  # nixPatches."2.31" is `[ ]` anyway -- no point rebuilding from
  # components (losing nixpkgs' own binary cache hit) just to apply zero
  # patches.
  #
  # `-tsan` suffixed entries are the same nix versions rebuilt with
  # ThreadSanitizer instrumentation (see tsanNixPackage above), each
  # patched per nixPatches/patchesFor exactly like their non-TSAN
  # counterparts (nix_2_31-tsan ends up unpatched the same way). They flow
  # through the exact same nanopynixVersions/dedup/test-exposure pipeline
  # below as first-class "supported" versions -- no separate scope to keep
  # in sync with the real ones.
  patchedNixVersions = pkgs.nixVersions // {
    nix_2_34 = patchNixComponents pkgs.nixVersions.nixComponents_2_34;
    nix_2_35 = patchNixComponents pkgs.nixVersions.nixComponents_2_35;
    git = patchNixComponents pkgs.nixVersions.nixComponents_git;

    nix_2_31-tsan = tsanNixPackage (
      pkgs.nixVersions.nixComponents_2_31.appendPatches (patchesFor pkgs.nixVersions.nixComponents_2_31)
    );
    nix_2_34-tsan = tsanNixPackage (
      pkgs.nixVersions.nixComponents_2_34.appendPatches (patchesFor pkgs.nixVersions.nixComponents_2_34)
    );
    nix_2_35-tsan = tsanNixPackage (
      pkgs.nixVersions.nixComponents_2_35.appendPatches (patchesFor pkgs.nixVersions.nixComponents_2_35)
    );
    git-tsan = tsanNixPackage (
      pkgs.nixVersions.nixComponents_git.appendPatches (patchesFor pkgs.nixVersions.nixComponents_git)
    );
  };

  # `-tsan` suffixed versions need nanopynix's own C++ bindings (not just
  # nix's libraries) built with matching ThreadSanitizer instrumentation --
  # see nix/tsan.nix and nanopynixForNixEx's `enableTsan` below -- so it's
  # derived from the name rather than threaded through as a separate flag
  # at every call site.
  nanopynixForNix =
    name: nix:
    nanopynixForNixEx {
      inherit nix;
      enableTsan = lib.hasSuffix "-tsan" name;
    };

  # `enableTsan` builds nix's own libraries (+ sqlite) and nanopynix's C++
  # bindings with ThreadSanitizer instrumentation instead of just nanopynix's
  # own code -- see nix/tsan.nix for which packages get instrumented and why.
  nanopynixForNixEx =
    {
      nix,
      enableTsan ? false,
    }:
    lib.makeScope
      (
        extra:
        lib.callPackageWith (
          pkgs
          // python3Packages
          // {
            inherit
              nanopynix-proto
              grpclib-transports
              clypi
              pyproject-nix
              renderPyproject
              renderEditablePyproject
              ;
          }
          // extra
        )
      )
      (self: {
        inherit nix;
        nanopynix-bindings = self.callPackage ./bindings/package.nix {
          inherit enableTsan;
          tsanRuntime = if enableTsan then tsan.tsanRuntime else null;
        };
        nanopynix = self.callPackage ./python/package.nix {
          inherit enableTsan;
          tsanRuntime = if enableTsan then tsan.tsanRuntime else null;
        };
        pynix = self.callPackage ./pynix/package.nix { };
        shell = self.callPackage ./nix/shell.nix { };
        tests = self.callPackage ./nix/tests.nix {
          inherit (inputs) nixpkgs;
          tsanRuntime = if enableTsan then tsan.tsanRuntime else null;
        };
        nanopynix-docs = self.callPackage ./nix/docs.nix { };
      });

  dedupeVersions = pkgs.callPackage ./nix/dedupe-nix-versions.nix { };

  nanopynixVersions = lib.pipe patchedNixVersions [
    (lib.filterAttrs (
      _: nix:
      let
        canEval = builtins.tryEval nix;
      in
      canEval.success && lib.isDerivation canEval.value
    ))
    (lib.mapAttrs nanopynixForNix)
  ];

  # Deduped view of nanopynixVersions used for test exposure/CI, so aliased
  # names (e.g. `stable`/`latest`) don't produce redundant test jobs. Note
  # `nanopynixVersions.stable` below is still the full, non-deduped set.
  nanopynixTestVersions = dedupeVersions nanopynixVersions;

  nanopynixVersionNames = builtins.attrNames nanopynixTestVersions;

  # Per-version test runners, exposed individually as `nanopynix-tests-<name>`
  # flake packages so CI can build/run each Nix version in its own job.
  nanopynixVersionTests = lib.mapAttrs' (
    name: value: lib.nameValuePair "nanopynix-tests-${name}" value.tests
  ) nanopynixTestVersions;

  nanopynix-all-tests = pkgs.callPackage ./nix/nix-version-tests.nix {
    nanopynixVersions = nanopynixTestVersions;
    inherit (inputs) nixpkgs;
  };

in
rec {
  inherit (pkgs) lib;

  inherit (nanopynixVersions.stable)
    nanopynix
    nanopynix-bindings
    pynix
    shell
    tests
    nanopynix-docs
    ;

  inherit
    flake
    pkgs
    nanopynixVersions
    nanopynixVersionNames
    nanopynix-all-tests
    nanopynix-proto
    clypi
    grpclib-transports
    pyproject-nix
    ;

  carlmazing = (
    tsan:
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

      patchNixScope =
        scope:
        scope.appendPatches (nixPatches.${lib.versions.majorMinor scope.version} or nixPatches.default);

      extendNixScope =
        scope:
        lib.makeScope
          (
            final:
            scope.newScope (
              lib.removeAttrs python3Packages [ "python3" ]
              // {
                inherit
                  grpclib-transports
                  renderPyproject
                  betterproto2
                  betterproto2-compiler
                  ;
              }
              // final
            )
          )
          (
            lib.extends (final: prev: {
              nanopynix-bindings = final.callPackage ./bindings/package.nix { };
              nanopynix = final.callPackage ./python/package.nix { };
            }) scope.packages
          );

      overrideMesonComponents = v: v;

      rename =
        name: value:
        let
          prefix = "nanopynix_";
          suffix = if tsan then "-tsan" else "";
        in
        lib.nameValuePair "${prefix}${lib.removePrefix "nixComponents_" name}${suffix}" value;
    in
    lib.pipe pkgs.nixVersions (
      [
        (lib.filterAttrs isNixScope)
        (lib.mapAttrs (_: patchNixScope))
        (lib.mapAttrs (_: extendNixScope))
        (lib.mapAttrs' rename)
      ]
      ++ lib.optional tsan (lib.mapAttrs (_: overrideMesonComponents))
    )
  );
  c2 = (carlmazing false) // (carlmazing true);
}
// nanopynixVersionTests
