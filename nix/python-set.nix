# A pyproject.nix *builders* package set for this repo, sourcing every
# third-party dependency from nixpkgs rather than from a lock file.
#
# Why the builders at all: nixpkgs' Python infrastructure works by dependency
# propagation, and `withPackages` keeps only derivations that are importable
# modules. A `buildPythonApplication` is therefore dropped from an environment
# *together with everything it propagates* -- which is how the test runner
# silently lost a native dependency reachable only through an application.
# pyproject.nix's builders put runtime dependencies in `passthru` instead and
# assemble real virtualenvs, so there is no propagation to fall off and no
# module/application distinction to be caught by.
#
# Why not uv2nix: the builders are written for lock-file consumers, and
# `pyproject-nix.build.packages` is by its own documentation "incomplete, and
# much smaller in both package count and scope than nixpkgs". Adopting a
# `uv.lock` would move every third-party dependency from nixpkgs to PyPI --
# including `pygit2`, `tree-sitter` and `kr8s`, which are native and which
# nixpkgs already builds correctly for us. So instead the set is populated
# *from* nixpkgs, via `hacks.nixpkgsPrebuilt`.
#
# What makes that affordable is `closure` below. `nixpkgsPrebuilt` normally
# takes its `passthru.dependencies` from a lock-file-derived `prev`; with no
# lock file there is no `prev`, and writing that metadata by hand for a
# transitive closure of this size would be its own maintenance burden. But
# nixpkgs already records exactly that relation, in `propagatedBuildInputs` --
# the very mechanism being replaced. So the closure is walked and the metadata
# derived from it, and adding a dependency to a pyproject.toml needs no edit
# here.
{
  lib,
  callPackage,
  pyproject-nix,
}:

let
  inherit (pyproject-nix.lib) pep508;

  # PyPI-style normalisation (PyPA name-normalization spec). nixpkgs mostly
  # agrees but has legacy names that don't, so this is best-effort and the
  # resolver will say plainly which name it could not find.
  normalize = name: lib.toLower (lib.replaceStrings [ "_" "." ] [ "-" "-" ] name);

  nameOf = pkg: normalize (pkg.pname or pkg.name);

  # The interpreter itself is a propagated input of most Python packages and
  # is not a dependency in the pyproject sense -- it is what the venv is
  # built *from*. Left in, it would recurse into the whole interpreter
  # closure and then fail to resolve as a package name.
  isPythonModule =
    pkg:
    lib.isDerivation pkg && pkg ? pythonModule && !(lib.hasPrefix "python3" (pkg.pname or pkg.name));

  propagatedOf = pkg: lib.filter isPythonModule (pkg.propagatedBuildInputs or [ ]);

  /*
    Every nixpkgs Python package reachable from `roots` by propagation,
    keyed by normalized name.

    Type: closure :: [derivation] -> AttrSet
  */
  closure =
    roots:
    let
      visit =
        acc: pkg:
        let
          key = nameOf pkg;
        in
        if acc ? ${key} then acc else lib.foldl' visit (acc // { ${key} = pkg; }) (propagatedOf pkg);
    in
    lib.foldl' visit { } (lib.filter isPythonModule roots);

  /*
    Lift one already-built nixpkgs Python package into a pyproject.nix
    builders set, deriving its runtime dependency metadata from the
    propagated inputs nixpkgs already recorded.

    `quiet` is set because the ABI warning it suppresses compares the
    interpreter of `from` against the one behind `prev`'s pyprojectHook --
    and `prev` here is a bare metadata attrset with no hook, so there is
    nothing to compare. `nixpkgsPrebuilt` reacts to that by setting its
    internal `python` to `null` (`build/hacks/default.nix:75`), which
    disables not just the warning but also the hard version check on the
    line below it. So both are reinstated here against `python` directly:
    a lifted package's `.so` files are linked against *its own*
    interpreter's ABI, and a silent mismatch shows up as an import error
    deep inside a venv rather than as an evaluation failure.

    The `installPhase` addendum is what makes a lifted package usable as a
    *build system* and not only as a runtime dependency. pyproject.nix
    assembles a build backend's `sys.path` from `NIX_PYPROJECT_PYTHONPATH`
    (`build/hooks/pyproject-build-hook.sh:15`), and each package contributes
    itself to that variable from its own `$out/nix-support/setup-hook`,
    written by `pyprojectOutputSetupHook`
    (`build/hooks/pyproject-output-setup-hook.sh:1-7`). `nixpkgsPrebuilt`
    never runs that hook -- its derivation carries no `pyprojectHook`
    (`build/hacks/default.nix:84-100`) and `write-nixpkgs-prebuilt.py`
    copies `nix-support` verbatim minus `propagated-build-inputs`
    (`write-nixpkgs-prebuilt.py:29-38`), and a nixpkgs Python package has
    nothing else in there. So a lifted package lands in `nativeBuildInputs`,
    contributes nothing to the search path, and the backend fails to import
    -- which is the `No module named 'hatchling'` above. Writing the same
    line `pyprojectOutputSetupHook` would have written fixes it.

    Type: liftNixpkgsPackage :: AttrSet -> derivation -> derivation
  */
  liftNixpkgsPackage =
    { hacks, python }:
    pkg:
    lib.throwIf (pkg.pythonModule.pythonVersion != python.pythonVersion)
      "Mismatching Python versions for ${pkg.name}: ${pkg.pythonModule.pythonVersion} != ${python.pythonVersion}"
      lib.warnIf
      (pkg.pythonModule != python)
      "Mismatching Python derivations for ${pkg.name}, beware of ABI compatibility issues"
      (hacks.nixpkgsPrebuilt {
        from = pkg;
        quiet = true;
        prev = {
          passthru = {
            dependencies = lib.listToAttrs (map (p: lib.nameValuePair (nameOf p) [ ]) (propagatedOf pkg));
            optional-dependencies = { };
          };
        };
      }).overrideAttrs
      (old: {
        installPhase = old.installPhase + ''
          mkdir -p "$out/nix-support"
          # Anything already in nix-support is a symlink into the nixpkgs
          # output; appending through it would try to write to the store.
          if [ -L "$out/nix-support/setup-hook" ]; then
            cp --remove-destination "$(readlink -f "$out/nix-support/setup-hook")" "$out/nix-support/setup-hook"
            chmod +w "$out/nix-support/setup-hook"
          fi
          cat >>"$out/nix-support/setup-hook" <<EOF
          addToSearchPath NIX_PYPROJECT_PYTHONPATH "$out/${pkg.pythonModule.sitePackages}"
          EOF
        '';
      });

in
# Four entry points, all of them used: two package-set constructors, the
# renderer wrapper each project goes through, and the roots reader that seeds
# a set. Everything above is internal to those.
{
  /*
    The nixpkgs packages a set of our own projects depend on, read out of
    their pyproject.toml files.

    Used to seed `mkPythonSet`'s `nixpkgsRoots`. The obvious alternative --
    asking the previously nixpkgs-built copies of our projects for their
    `propagatedBuildInputs` -- is circular once those projects are built by
    the builders set instead, and would also mean the roots were derived from
    a build we are in the middle of replacing. Reading the declarations is
    both acyclic and closer to the truth.

    The projects' own names are never looked up: a project is supplied by the
    overlay, not by nixpkgs, and they depend on each other. `exclude` is for
    anything *else* the overlay provides. Everything remaining is resolved
    against `python.pkgs`, which is where a name is expected to be resolvable
    -- an unresolvable one is a real error and says so, rather than being
    silently dropped and reappearing as an import failure inside a venv.

    Type: nixpkgsRootsFor :: AttrSet -> [derivation]
  */
  nixpkgsRootsFor =
    {
      python,
      projectRoots,
      exclude ? [ ],
    }:
    let
      declaredNames =
        projectRoot:
        let
          inherit (pyproject-nix.lib.project.loadPyproject { inherit projectRoot; }) dependencies;
        in
        map (dep: normalize dep.name) (
          dependencies.dependencies
          ++ dependencies.build-systems
          ++ lib.concatLists (lib.attrValues dependencies.extras)
        );

      wanted = lib.subtractLists (map normalize (exclude ++ map baseNameOf projectRoots)) (
        lib.unique (lib.concatMap declaredNames projectRoots)
      );
    in
    map (
      name:
      python.pkgs.${name} or (throw "no nixpkgs Python package `${name}`, declared by one of ${
        lib.concatMapStringsSep ", " toString projectRoots
      }")
    ) wanted;

  /*
    A builders package set for `python`, containing the transitive closure of
    every nixpkgs package in `nixpkgsRoots` plus whatever `overlay` adds on
    top (this repo's own projects).

    Type: mkPythonSet :: AttrSet -> AttrSet
  */
  mkPythonSet =
    {
      python,
      # nixpkgs Python packages to source from nixpkgs, with their closures
      nixpkgsRoots,
      # extra packages, as a standard overlay -- this repo's own projects
      overlay ? (_final: _prev: { }),
    }:
    let
      hacks = callPackage pyproject-nix.build.hacks { };
      base = callPackage pyproject-nix.build.packages { inherit python; };

      # `base` is scaffolding only -- hooks, resolvers, `mkVirtualEnv`, and no
      # Python packages at all (pyproject.nix's own `doc/src/builders/
      # packages.md` says so in its first line; the build-systems it "ships"
      # live in a separate flake, pyproject-nix/build-system-pkgs, which is a
      # PyPI-sourced set we deliberately don't take). So the filter below
      # never actually excludes a package; it only keeps the lift from
      # shadowing an infrastructure attribute should nixpkgs ever grow a
      # Python package called e.g. `python` or `hooks`.
      # The builders' *own* editable hook is itself built as a venv out of
      # this set (`build/hooks/editable_hook/default.nix`), so `libcst` --
      # which its `patch-editable` script imports -- has to be resolvable
      # here even though nothing in this repo depends on it. A lock-file
      # consumer never notices, because a `uv.lock` covering an editable
      # install already contains libcst. Sourcing it from nixpkgs like
      # everything else keeps that true for us.
      infraRoots = [ python.pkgs.libcst ] ++ lib.optional (python.pythonOlder "3.11") python.pkgs.tomli;

      lifted = lib.mapAttrs (_: liftNixpkgsPackage { inherit hacks python; }) (
        lib.filterAttrs (name: _: !(base.pythonPkgsHostHost ? ${name})) (
          closure (nixpkgsRoots ++ infraRoots)
        )
      );
    in
    base.pythonPkgsHostHost.overrideScope (lib.composeExtensions (_final: _prev: lifted) overlay);

  /*
    One of this repo's own pyproject.toml projects, as a function ready for
    `pythonSet.callPackage`.

    The renderer is invoked directly with the two hooks rather than through
    `callPackage`, which is what uv2nix does (`lib/build.nix:196`) and is not
    a style choice: `callPackage` returns its result wrapped by
    `makeOverridable`, so the attrset picks up `override` and
    `overrideDerivation`. Handing that to `stdenv.mkDerivation` makes it try
    to turn those functors into environment variables and fail with
    `cannot coerce a set to a string`.

    `extra` receives the rendered attrs so a caller can append to
    `nativeBuildInputs` or extend `passthru` rather than silently replacing
    what the renderer derived from pyproject.toml.

    Type: mkProject :: AttrSet -> (AttrSet -> derivation)
  */
  mkProject =
    {
      projectRoot,
      python,
      extra ? (_rendered: { }),
    }:
    {
      stdenv,
      pyprojectHook,
      resolveBuildSystem,
    }:
    let
      rendered =
        (pyproject-nix.build.lib.renderers.mkDerivation {
          project = pyproject-nix.lib.project.loadPyproject { inherit projectRoot; };
          environ = pep508.mkEnviron python;
        })
          {
            inherit pyprojectHook resolveBuildSystem;
          };
    in
    stdenv.mkDerivation (rendered // extra rendered);

  /*
    The same, as an *editable* install: the project's `src` is left in the
    working tree and the venv gets a path hook pointing back at it, so an
    edit is live without a rebuild.

    This is what the dev shell and the test runner install our own projects
    as. Under the nixpkgs builders that role was filled by `pytest.ini`'s
    `pythonpath`, which worked but meant the tests were importing something
    the packaging had never built -- a project could pass its whole suite and
    still not be installable. An editable install is the same liveness with
    the real dependency graph behind it.

    Type: mkEditableProject :: AttrSet -> (AttrSet -> derivation)
  */
  mkEditableProject =
    {
      projectRoot,
      # Absolute path to the source root, as a string. Not a `path` value:
      # that would copy the tree into the store and defeat the point.
      root,
      python,
      extra ? (_rendered: { }),
    }:
    {
      stdenv,
      pyprojectEditableHook,
      resolveBuildSystem,
    }:
    let
      rendered =
        (pyproject-nix.build.lib.renderers.mkDerivationEditable {
          project = pyproject-nix.lib.project.loadPyproject { inherit projectRoot; };
          environ = pep508.mkEnviron python;
          inherit root;
        })
          {
            inherit pyprojectEditableHook resolveBuildSystem;
          };
    in
    stdenv.mkDerivation (rendered // extra rendered);
}
