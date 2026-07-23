{
  lib,
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
  nanopynix = python.pkgs.mkPythonEditablePackage (renderEditablePyproject {
    projectRoot = ../nanopynix;
    root = "$GIT_ROOT/nanopynix/src";
    inherit python;
    pythonPackages = python.pkgs // {
      inherit nanopynix-bindings nanopynix-proto grpclib-transports;
    };
  });

  nanopynix-helpers = python.pkgs.mkPythonEditablePackage (renderEditablePyproject {
    projectRoot = ../nanopynix-helpers;
    root = "$GIT_ROOT/nanopynix-helpers/src";
    inherit python;
    pythonPackages = python.pkgs // {
      inherit nanopynix tree-sitter-nix;
    };
  });

  pynix = python.pkgs.mkPythonEditablePackage (renderEditablePyproject {
    projectRoot = ../pynix;
    root = "$GIT_ROOT/pynix/src";
    inherit python;
    extras = [ "test" ];
    pythonPackages = python.pkgs // {
      inherit nanopynix nanopynix-helpers clypi tree-sitter-nix;
    };
  });

  pytest-agent = python.pkgs.mkPythonEditablePackage (renderEditablePyproject {
    projectRoot = ../pytest-agent;
    root = "$GIT_ROOT/pytest-agent/src";
    inherit python;
  });

  pythonEnv = python.withPackages (
    pp:
    nanopynix.dependencies
    ++ nanopynix-helpers.dependencies
    ++ pynix.dependencies
    ++ pytest-agent.dependencies
    ++ [
      nanopynix
      nanopynix-helpers
      pynix
      pytest-agent
      sphinx
      myst-parser
      furo
    ]
  );
in
mkShell {
  shellHook = ''
    export GIT_ROOT=${lib.escapeShellArg (toString ../.)}
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
