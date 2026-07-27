{
  mkShell,
  python,
  pyright,
  ruff,
  nixfmt,
  clang-tools,
  taplo,
  treefmt,
  actionlint,
  renderEditablePyproject,
  sphinx,
  myst-parser,
  furo,
  cachix,
  statix,
  tofuCoreSchemaTool,
  gdb,
}:
let
  # Reuses the same editable package definitions dev-env.nix exports (rather
  # than its combined `pythonEnv`) so this shell's own extra docs deps
  # (sphinx/myst-parser/furo) land in the *same* python.withPackages env --
  # two separate envs on PATH would collide on bin/python3 et al.
  # Only the two arguments dev-env.nix actually takes: every Python package
  # it needs now resolves through `python.pkgs`, which is the point of having
  # one package set.
  devEnv = import ./dev-env.nix {
    inherit python renderEditablePyproject;
  };

  pythonEnv = python.withPackages (
    pp:
    devEnv.nanopynix.dependencies
    ++ devEnv.nanopynix-helpers.dependencies
    ++ devEnv.pynix.dependencies
    ++ devEnv.ekn.dependencies
    ++ devEnv.pytest-agent.dependencies
    ++ [
      devEnv.nanopynix
      devEnv.nanopynix-helpers
      devEnv.pynix
      devEnv.ekn
      devEnv.pytest-agent
      sphinx
      myst-parser
      furo
    ]
  );
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
    cachix
    statix
    gdb
    # pynix._lsp._tofu_core_schema invokes this at LSP-server runtime (see
    # its module docstring) rather than baking a static snapshot -- on PATH
    # here so the editable dev shell resolves it exactly like the real,
    # non-editable build's makeWrapperArgs does (pynix/package.nix).
    tofuCoreSchemaTool
  ];
}
