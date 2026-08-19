{
  mkShell,
  pyright,
  ruff,
  nixfmt,
  clang-tools,
  fish,
  zsh,
  ncurses,
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
    # The terminfo database of this closure, for the pty driver. A shell that
    # cannot find the `xterm` entry draws no candidate list, and fish draws
    # none at all. Appended, so a terminal emulator that already named a
    # directory keeps it.
    export TERMINFO_DIRS="''${TERMINFO_DIRS:+$TERMINFO_DIRS:}${ncurses}/share/terminfo"
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

    # **The three shells that two suites drive on a pty.**
    # `completion-spike` and `pynix/completions/tests/` press Tab in fish, bash
    # and zsh and read back what each one offered. bash comes from
    # `suiteRuntime` above. Without these, both suites skip in the dev shell
    # and run in the Nix gates alone, so a developer would first learn of a
    # broken completion from CI.
    #
    # `ncurses` carries the terminfo database. The driver asks for an `xterm`
    # terminal, because fish draws no candidate list at all on a terminal it
    # believes cannot address the cursor.
    fish
    zsh
    ncurses
  ];
}
