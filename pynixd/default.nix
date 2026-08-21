{
  pkgs,
  nanopython,
}:
let
  inherit (pkgs) lib;

  nanoPythonPackages = pkgs.nanoPythonPackages or pkgs.python315Packages;

  daemon-protocol = nanoPythonPackages.callPackage ./nix/nix-daemon-protocol.nix {
    pythonBuilder = nanoPythonPackages.buildPythonPackage;
  };

  package = nanoPythonPackages.callPackage ./nix/pynixd.nix {
    pythonBuilder = nanoPythonPackages.buildPythonApplication;
    nix-daemon-protocol = daemon-protocol;
  };
  library = nanoPythonPackages.callPackage ./nix/pynixd.nix {
    pythonBuilder = nanoPythonPackages.buildPythonPackage;
    nix-daemon-protocol = daemon-protocol;
  };

  mkTests =
    {
      name,
      testArgs,
    }:
    pkgs.writeShellApplication {
      inherit name;
      runtimeInputs = [
        (nanopython.withPackages (ps: [
          library
          ps.pytest
          ps.pyinstrument
        ]))
      ];
      text = ''
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
      pyinstance = pkgs.python3.withPackages (
        ps:
        [ library ]
        ++ library.dependencies
        ++ [
          ps.pytest
        ]
      );
    in
    pkgs.writeShellApplication {
      name = "pynixd-lint";
      runtimeInputs = [
        pyinstance
        pkgs.pyright
        pkgs.ruff
      ];
      text = ''
        src=${toString ./pynixd}
        echo "=== ruff fmt ==="
        ruff format "$src" ./tests || true
        echo "=== ruff check ==="
        ruff check --fix "$src" ./tests || true
        echo "=== pyright ==="
        pyright --pythonpath ${pyinstance}/bin/python "$src" ./tests || true
      '';
    };
in
package
// {
  inherit
    package
    library
    daemon-protocol
    specifictest
    lint
    pkgs
    ;

  pynixd-docs = nanoPythonPackages.callPackage ./nix/docs.nix { pynixd = library; };

  shell = pkgs.callPackage ./nix/shell.nix { pynixd = package; };
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
