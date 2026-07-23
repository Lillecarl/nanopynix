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
  nanopynix-bindings,
  nanopynix-proto,
  grpclib-transports,
  clypi,
  kr8s,
  renderEditablePyproject,
  sphinx,
  myst-parser,
  furo,
  cachix,
  statix,
  tofuCoreSchemaTool,
  tree-sitter-nix,
}:
let
  # Reuses the same editable package definitions dev-env.nix exports (rather
  # than its combined `pythonEnv`) so this shell's own extra docs deps
  # (sphinx/myst-parser/furo) land in the *same* python.withPackages env --
  # two separate envs on PATH would collide on bin/python3 et al.
  devEnv = import ./dev-env.nix {
    inherit
      python
      nanopynix-bindings
      nanopynix-proto
      grpclib-transports
      clypi
      kr8s
      tree-sitter-nix
      renderEditablePyproject
      ;
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
    # pynix._lsp._tofu_core_schema invokes this at LSP-server runtime (see
    # its module docstring) rather than baking a static snapshot -- on PATH
    # here so the editable dev shell resolves it exactly like the real,
    # non-editable build's makeWrapperArgs does (pynix/package.nix).
    tofuCoreSchemaTool
  ];
}
