{
  python,
  nanopynix-bindings,
  nanopynix-proto,
  grpclib-transports,
  clypi,
  kr8s,
  tree-sitter-nix,
  renderEditablePyproject,
}:
let
  nanopynix = python.pkgs.mkPythonEditablePackage (renderEditablePyproject {
    projectRoot = ../nanopynix;
    root = "$NANOPYNIX_GIT_ROOT/nanopynix/src";
    inherit python;
    pythonPackages = python.pkgs // {
      inherit nanopynix-bindings nanopynix-proto grpclib-transports;
    };
  });

  nanopynix-helpers = python.pkgs.mkPythonEditablePackage (renderEditablePyproject {
    projectRoot = ../nanopynix-helpers;
    root = "$NANOPYNIX_GIT_ROOT/nanopynix-helpers/src";
    inherit python;
    pythonPackages = python.pkgs // {
      inherit nanopynix tree-sitter-nix;
    };
  });

  pynix = python.pkgs.mkPythonEditablePackage (renderEditablePyproject {
    projectRoot = ../pynix;
    root = "$NANOPYNIX_GIT_ROOT/pynix/src";
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
    root = "$NANOPYNIX_GIT_ROOT/ekn/src";
    inherit python;
    pythonPackages = python.pkgs // {
      inherit nanopynix nanopynix-helpers clypi kr8s;
    };
  });

  pytest-agent = python.pkgs.mkPythonEditablePackage (renderEditablePyproject {
    projectRoot = ../pytest-agent;
    root = "$NANOPYNIX_GIT_ROOT/pytest-agent/src";
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
  # by editable installs -- consumers must `export NANOPYNIX_GIT_ROOT=<path
  # to a nanopynix checkout>` before running anything from this env's `bin/`
  # (mkPythonEditablePackage's generated loader calls
  # `os.path.expandvars("$NANOPYNIX_GIT_ROOT/.../src")` at *import* time, not
  # build time -- see pyproject-nix's editable_hook). Exported so other
  # repos (e.g. hetzkube) can drop a live, hot-reloading `pynix ekn` into
  # their own devShell/direnv setup without rebuilding on every edit. Does
  # not include nanopynix's own devtools (pyright/ruff/...) -- see
  # nix/shell.nix for the full interactive nanopynix dev shell.
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
