{
  pkgs ?
    let
      inputs =
        (
          let
            lock = builtins.fromJSON (builtins.readFile ./flake.lock);
            flake-compatish = import (fetchTree lock.nodes.flake-compatish.locked);
          in
          flake-compatish {
            source = ./.;
            overrides = {
              self = ./.;
            };
          }
        ).inputs;

      nixPath = builtins.tryEval (import <nixpkgs> { });
      nixInputs = import inputs.nixpkgs { };
    in
    if nixPath.success then nixPath.value else nixInputs,
}:
let
  inherit (pkgs) lib;

  python = pkgs.python3;

  commonAttrs = {
    pname = "pynixd";
    version = "0.1.0";
    pyproject = true;

    src = lib.cleanSource ./.;

    build-system = [
      python.pkgs.hatchling
    ];

    dependencies = [
      # python.pkgs.asyncssh
      (python.pkgs.asyncssh.overrideAttrs {
        src = fetchTree {
          type = "github";
          repo = "asyncssh";
          owner = "ronf";
        };
        doCheck = false;
        doInstallCheck = false;
      })
      python.pkgs.structlog
      python.pkgs.rich
      python.pkgs.aiohttp
      python.pkgs.pyinstrument
      python.pkgs.aiosqlite
      python.pkgs.environs
      python.pkgs.pynacl
      python.pkgs.passlib
      python.pkgs.cachetools
      python.pkgs.zstandard
      python.pkgs.lz4
      python.pkgs.brotli
      python.pkgs.pydantic
      python.pkgs.pydantic-settings
      python.pkgs.prometheus-client
      python.pkgs.anyio
      python.pkgs.tenacity
    ];

    nativeCheckInputs = [
      python.pkgs.pytest
      python.pkgs.pytest-asyncio
      python.pkgs.pytest-timeout
    ];

    meta = {
      description = "Python Nix daemon protocol proxy over SSH";
      mainProgram = "pynixd";
    };
  };

  package = python.pkgs.buildPythonApplication commonAttrs;
  library = python.pkgs.buildPythonPackage commonAttrs;

  mkTests =
    {
      name,
      testArgs,
    }:
    pkgs.writeShellApplication {
      inherit name;
      runtimeInputs = [
        (python.withPackages (ps: [
          library
          ps.pytest
          ps.pytest-asyncio
          ps.pytest-timeout
          ps.pyinstrument
        ]))
      ];
      text = ''
        export LIX_BIN=${lib.getExe pkgs.lix}
        export NIX_BIN=${lib.getExe pkgs.nix}
        exec pytest -p no:cacheprovider --timeout=60 ${testArgs} "$@"
      '';
    };

  specifictest = mkTests {
    name = "pynixd-specifictest";
    testArgs = "";
  };
  lint =
    let
      pyinstance = python.withPackages (
        ps:
        [ library ]
        ++ library.dependencies
        ++ [
          ps.pytest
          ps.pytest-asyncio
          ps.pytest-timeout
        ]
      );
    in
    pkgs.writeShellApplication {
      name = "pynixd-lint";
      runtimeInputs = [
        pyinstance
        pkgs.pyright
        pkgs.ruff
        pkgs.ty
        # pkgs.zuban
      ];
      text = ''
        src=${toString ./pynixd}
        echo "=== ruff fmt ==="
        ruff format "$src" ./tests || true
        echo "=== ruff check ==="
        ruff check --fix "$src" ./tests || true
        echo "=== pyright ==="
        pyright --pythonpath ${pyinstance}/bin/python "$src" ./tests || true
        echo "=== ty ==="
        ty check --python ${pyinstance}/bin/python "$src" ./tests || true
        # echo "=== zuban ==="
        # zuban check --follow-untyped-imports --python-executable ${pyinstance}/bin/python "$src" || true
      '';
    };
in
package
// {
  inherit
    package
    library
    specifictest
    lint
    pkgs
    ;

  nixosModule = import ./nix/nixos/default.nix;

  tests = {
    simple = pkgs.callPackage ./tests/derivations/simple {
      pynixd-lib = library;
    };
    pytest = pkgs.callPackage ./tests/derivations/pytest {
      pynixd-lib = library;
      src = lib.cleanSource ./.;

    };
  };
}
