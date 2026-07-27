{
  python,
  renderEditablePyproject,
}:
let
  # The one place in this repo where an explicit `python.pkgs // { ... }` is
  # right rather than an accident.
  #
  # Everywhere else the interpreter's own package set already holds our
  # packages, so a splice would only be a second, divergent copy of that
  # list. Here the job is the opposite: *shadow* each built package with its
  # editable build, so that a cross-reference between two of our projects
  # resolves to the editable copy on both sides.
  #
  # That matters because `mkPythonEditablePackage` eagerly resolves every
  # declared `[project.optional-dependencies]` name for the editable
  # install's metadata, whatever `extras` says -- so pynix's `ekn` extra is
  # looked up regardless. Against the plain set it would find the *built*
  # ekn and quietly pin the dev shell to a store copy, defeating the point of
  # ekn and pynix sharing one venv: ekn's source can cross-import pynix's
  # modules and vice versa, which a real application-to-application
  # dependency cannot do without a derivation cycle (see pynix/package.nix's
  # `ekn` argument, which bundles the built one for non-dev use).
  #
  # One set, referenced by every project below, rather than the four
  # hand-maintained ones this replaced -- a name missing from one of those
  # was not an error, just a silent fallback to the built package.
  pythonPackages = python.pkgs // {
    inherit
      nanopynix
      nanopynix-helpers
      pynix
      ekn
      pytest-agent
      ;
  };

  editable =
    name:
    {
      extras ? [ ],
    }:
    python.pkgs.mkPythonEditablePackage (renderEditablePyproject {
      projectRoot = ../. + "/${name}";
      # `toString`'d absolute path into this checkout, not a store copy -- see
      # the note on pythonEnv below for why that is load-bearing.
      root = toString (../. + "/${name}/src");
      inherit python pythonPackages extras;
    });

  nanopynix = editable "nanopynix" { };
  nanopynix-helpers = editable "nanopynix-helpers" { };
  ekn = editable "ekn" { };
  pynix = editable "pynix" { extras = [ "test" ]; };
  pytest-agent = editable "pytest-agent" { };
in
{
  inherit
    nanopynix
    nanopynix-helpers
    pynix
    ekn
    pytest-agent
    ;

  # A `pynix`/`ekn` (plus the plain `python3` interpreter) backed entirely
  # by editable installs -- each package's `root` above is this checkout's
  # own `toString`'d absolute path (no store copy, since this whole tree is
  # consumed via a local path override rather than flake eval), so the
  # generated loaders resolve straight back here at import time with
  # nothing for a consumer to export. Exported so other repos (e.g.
  # hetzkube) can drop a live, hot-reloading `pynix ekn` into their own
  # devShell/direnv setup without rebuilding on every edit. Does not include
  # nanopynix's own devtools (pyright/ruff/...) -- see nix/shell.nix for the
  # full interactive nanopynix dev shell.
  #
  # Each project contributes `.dependencies` as well as itself, and that is
  # not redundant: `withPackages` keeps only importable modules, so an
  # application is dropped from the environment *together with everything it
  # propagates*. Listing only the packages is how the test runner lost
  # `kr8s`.
  pythonEnv = python.withPackages (
    _pp:
    nanopynix.dependencies
    ++ nanopynix-helpers.dependencies
    ++ pynix.dependencies
    ++ ekn.dependencies
    ++ pytest-agent.dependencies
    ++ [
      nanopynix
      nanopynix-helpers
      pynix
      ekn
      pytest-agent
    ]
  );
}
