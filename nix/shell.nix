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
}:
let
  nanopynix = python.pkgs.mkPythonEditablePackage (renderEditablePyproject {
    projectRoot = ../nanopynix;
    root = "$GIT_ROOT/nanopynix/src";
    inherit python;
    pythonPackages = python.pkgs // {
      "nanopynix-bindings" = nanopynix-bindings;
      "nanopynix-proto" = nanopynix-proto;
      "grpclib-transports" = grpclib-transports;
    };
  });

  pynix = python.pkgs.mkPythonEditablePackage (renderEditablePyproject {
    projectRoot = ../pynix;
    root = "$GIT_ROOT/pynix/src";
    inherit python;
    extras = [ "test" ];
    pythonPackages = python.pkgs // {
      inherit nanopynix clypi;
      "tree-sitter-nix" = python.pkgs.tree-sitter-grammars.tree-sitter-nix.overridePythonAttrs (_: {
        pname = "tree-sitter-nix";
      });
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
    ++ pynix.dependencies
    ++ pytest-agent.dependencies
    ++ [
      nanopynix
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
  ];
}
