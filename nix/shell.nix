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
  storeExecTools,
  gdb,
  # The `nix` of this scope, which is the Nix that the bindings of this shell
  # link. `packages` below puts it first on PATH. See the note there.
  nix-cli,
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
    # **The `nix` on PATH here is the Nix that these bindings link.**
    #
    # Two tests compare pynix against the `nix` command: `test_flake_metadata`
    # runs `nix flake metadata`, and `test_develop` runs `nix print-dev-env`.
    # Both read the binary from PATH, so a machine whose own Nix is newer than
    # this checkout links makes them compare two Nix releases rather than
    # pynix. Measured: a 2.35.1 installation against the 2.34.8 of these
    # bindings reports a different flake `fingerprint`, and the test says only
    # that two hashes differ.
    #
    # `ci/steps.nix` leaves `nix` out of every `runtimeInputs` for the opposite
    # reason, and both are right. A runner installs one Nix and that
    # installation owns the store the tests write to, so a second copy there
    # would be redundant. A developer machine owns a Nix that nothing here
    # chose, which is the case this fixes.
    #
    # `support/nix_oracle.py` still checks, because a shell is not the only way
    # to run the suite.
    nix-cli
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
  ]
  ++ storeExecTools;
}
