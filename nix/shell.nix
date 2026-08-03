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
  tofuCoreSchemaTool,
  storeExecTool,
  gdb,
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
        "ekn"
        "docs"
      ];
    };
  };
in
mkShell {
  shellHook = ''
    unset PYTHONPATH
  '';

  packages = [
    pythonEnv
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
    gdb
    # pynix._lsp._tofu_core_schema invokes this at LSP-server runtime (see
    # its module docstring) rather than baking a static snapshot -- on PATH
    # here so the editable dev shell resolves it exactly like the real,
    # non-editable build's makeWrapperArgs does (pynix/package.nix).
    tofuCoreSchemaTool
    storeExecTool
  ];
}
