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
  nix,
  sphinx,
  myst-parser,
  furo,
}:
let
  nanopynix = python.pkgs.mkPythonEditablePackage (renderEditablePyproject {
    projectRoot = ../python;
    root = "$GIT_ROOT/python/src";
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

  pythonEnv = python.withPackages (
    pp:
    nanopynix.dependencies
    ++ pynix.dependencies
    ++ [
      nanopynix
      pynix
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
    nix
    pyright
    ruff
    nixfmt
    clang-tools
    taplo
    treefmt
    actionlint
  ];
}
