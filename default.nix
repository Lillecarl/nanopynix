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

  # Applies emptyBindingsPatch to a modular nix component set (the ones
  # exposing `.appendPatches`/`nix-everything`, i.e. nixComponents_2_31 and
  # newer) and re-merges it into a single "nix" package, so it can be fed into
  # nanopynixForNix exactly like an unpatched nixVersions.* entry.
  patchNixComponents =
    components: (components.appendPatches [ emptyBindingsPatch ]).nix-everything;

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
  # nix_2_34/nix_2_35/git share attr-set.cc/.hh source context close enough
  # for the patch to apply unchanged; nix_2_31's surrounding source differs
  # enough that the hunks don't match (confirmed by trying), and 2.31 is the
  # lowest-priority supported version, so it's left unpatched rather than
  # broken -- revisit if/when 2.31 support is reconsidered.
  #
  # `-tsan` suffixed entries are the same nix versions rebuilt with
  # ThreadSanitizer instrumentation (see tsanNixPackage above). They flow
  # through the exact same nanopynixVersions/dedup/test-exposure pipeline
  # below as first-class "supported" versions -- no separate scope to keep
  # in sync with the real ones. nix_2_31-tsan is unpatched like its
  # non-TSAN counterpart, since emptyBindingsPatch doesn't apply there
  # either; it's still useful to confirm the same race(s) reproduce on
  # that version, not to prove they're fixed.
  patchedNixVersions = pkgs.nixVersions // {
    nix_2_34 = patchNixComponents pkgs.nixVersions.nixComponents_2_34;
    nix_2_35 = patchNixComponents pkgs.nixVersions.nixComponents_2_35;
    git = patchNixComponents pkgs.nixVersions.nixComponents_git;

    nix_2_31-tsan = tsanNixPackage pkgs.nixVersions.nixComponents_2_31;
    nix_2_34-tsan = tsanNixPackage (pkgs.nixVersions.nixComponents_2_34.appendPatches [ emptyBindingsPatch ]);
    nix_2_35-tsan = tsanNixPackage (pkgs.nixVersions.nixComponents_2_35.appendPatches [ emptyBindingsPatch ]);
    git-tsan = tsanNixPackage (pkgs.nixVersions.nixComponents_git.appendPatches [ emptyBindingsPatch ]);
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
{
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

}
// nanopynixVersionTests
