{
  python,
  nanopynix-bindings,
  nanopynix-proto,
  grpclib-transports,
  clypi,
  kr8s,
  pytest-beartype,
  tree-sitter-nix,
  renderEditablePyproject,
}:
let
  nanopynix = python.pkgs.mkPythonEditablePackage (renderEditablePyproject {
    projectRoot = ../nanopynix;
    root = toString (../nanopynix + "/src");
    inherit python;
    pythonPackages = python.pkgs // {
      inherit
        nanopynix-bindings
        nanopynix-proto
        grpclib-transports
        pytest-beartype
        ;
    };
  });

  nanopynix-helpers = python.pkgs.mkPythonEditablePackage (renderEditablePyproject {
    projectRoot = ../nanopynix-helpers;
    root = toString (../nanopynix-helpers + "/src");
    inherit python;
    pythonPackages = python.pkgs // {
      inherit nanopynix tree-sitter-nix;
    };
  });

  pynix = python.pkgs.mkPythonEditablePackage (renderEditablePyproject {
    projectRoot = ../pynix;
    root = toString (../pynix + "/src");
    inherit python;
    # `extras` only controls which extras are *installed*; mkPythonEditablePackage
    # still eagerly resolves every declared [project.optional-dependencies]
    # group's package names against pythonPackages regardless (to build the
    # editable install's metadata) -- so `ekn` (pynix's `ekn` extra) has to
    # be resolvable here even though it isn't listed in `extras` below.
    extras = [ "test" ];
    pythonPackages = python.pkgs // {
      inherit
        nanopynix
        nanopynix-helpers
        clypi
        tree-sitter-nix
        ekn
        ;
    };
  });

  # Editable, in the same shared venv as pynix -- lets ekn's source freely
  # cross-import pynix's modules (and vice versa) during interactive dev,
  # which a real buildPythonApplication-to-buildPythonApplication dependency
  # can't do without a Nix derivation cycle (see pynix/package.nix's `ekn`
  # arg, which bundles the *built* ekn instead).
  ekn = python.pkgs.mkPythonEditablePackage (renderEditablePyproject {
    projectRoot = ../ekn;
    root = toString (../ekn + "/src");
    inherit python;
    pythonPackages = python.pkgs // {
      inherit
        nanopynix
        nanopynix-helpers
        clypi
        kr8s
        ;
    };
  });

  pytest-agent = python.pkgs.mkPythonEditablePackage (renderEditablePyproject {
    projectRoot = ../pytest-agent;
    root = toString (../pytest-agent + "/src");
    inherit python;
  });
in
{
  inherit
    nanopynix
    nanopynix-helpers
    pynix
    ekn
    pytest-agent
    ;

  # A `pynix`/`ekn` (plus the plain `python3` interpreter) backed entirely
  # by editable installs -- each package's `root` above is this checkout's
  # own `toString`'d absolute path (no store copy, since this whole tree is
  # consumed via a local path override rather than flake eval), so the
  # generated loaders resolve straight back here at import time with
  # nothing for a consumer to export. Exported so other repos (e.g.
  # hetzkube) can drop a live, hot-reloading `pynix ekn` into their own
  # devShell/direnv setup without rebuilding on every edit. Does not include
  # nanopynix's own devtools (pyright/ruff/...) -- see nix/shell.nix for the
  # full interactive nanopynix dev shell.
  pythonEnv = python.withPackages (
    pp:
    nanopynix.dependencies
    ++ nanopynix-helpers.dependencies
    ++ pynix.dependencies
    ++ ekn.dependencies
    ++ pytest-agent.dependencies
    ++ [
      nanopynix
      nanopynix-helpers
      pynix
      ekn
      pytest-agent
    ]
  );
}
