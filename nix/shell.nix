{
  mkShell,
  pyright,
  ruff,
  nixfmt,
  clang-tools,
  taplo,
  treefmt,
  actionlint,
  shellcheck,
  editablePythonSet,
  cachix,
  statix,
  suiteRuntime,
}:
let
  # Reuses dev-env.nix's editable venv rather than assembling a second one.
  # The docs toolchain goes *into* that venv, via pynix's `docs` extra, rather
  # than onto PATH beside it: `sphinx-build` has to import what it documents,
  # and a second Python environment would have its own `sys.path` without it.
  pythonEnv = import ./dev-env.nix {
    inherit editablePythonSet;
    extraSpec = {
      pynix = [
        "test"
        "docs"
      ];
    };
  };
in
mkShell {
  shellHook = ''
    unset PYTHONPATH
  '';

  # `suiteRuntime` is the list that `nanopynix/tests.nix` also takes, so a tool
  # the suite needs cannot reach the dev shell without reaching the packaged
  # runner as well. That drift cost two debugging sessions -- see the file.
  #
  # Everything after it is development tooling, which the runner must not
  # carry: a gate belongs in `nix/checks.nix`, not on the PATH of a test run.
  packages = [
    pythonEnv
  ]
  ++ suiteRuntime
  ++ [
    pyright
    ruff
    nixfmt
    clang-tools
    taplo
    treefmt
    actionlint
    # `scripts/` holds shell, and `writeShellApplication` only checks the
    # scripts that it generates. Without this the dev shell cannot run the
    # `check-shell` gate that nix/checks.nix now builds.
    shellcheck
    cachix
    statix
  ];
}
