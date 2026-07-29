# The static gates, as derivations, so CI can fail on them.
#
# Four commands that every contributor is asked to keep clean and that nothing
# enforced. `ruff-strict.toml` in particular is a large, considered
# configuration whose whole value is that it reports nothing, and until this
# existed the only thing keeping it at zero was habit.
#
# One derivation each rather than one that runs all four, so a failing run
# names the gate that failed, and so the three cheap ones still report when
# pyright is the slow one.
{
  lib,
  runCommand,
  ruff,
  pyright,
  pythonSet,
}:
let
  # Only the trees the gates read. `${../.}` -- what the test runner uses --
  # would drag `.pytest-agent`, `result` symlinks and `.direnv` in under a
  # plain `--file .` evaluation, and a lint gate must not report on files that
  # are gitignored and absent under a flake evaluation. Naming the inputs also
  # keeps an unrelated edit from rebuilding all four.
  source = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      # `pyproject.toml` carries the pyright configuration, and `ruff.toml`
      # carries the default ruff one. Omitting `ruff.toml` does not fail the
      # build -- ruff silently falls back to its own defaults, which select
      # fewer rules and assume an older Python. Measured: the first build of
      # this file reported three `F821 Undefined name BaseExceptionGroup`,
      # because the fallback target is older than the `py313` that
      # `ruff.toml` sets.
      ../pyproject.toml
      ../ruff.toml
      ../ruff-strict.toml
      ../nanopynix
      ../nanopynix-bindings
      ../nanopynix-helpers
      ../nanopynix-proto
      ../pynix
      ../ekn
      ../pytest-agent
      ../tests
      ../tools
      ../docs
      ../ci
      # Tracked, and therefore in scope, although it holds one module and no
      # project of its own. With it the gate reads 259 Python files, which is
      # what `ruff format --check .` reads in the dev shell. Without it the
      # gate reads fewer, and a hole in a gate is worse than no gate.
      ../tmp
    ];
  };

  # Not editable, and the same reasoning as the test runner: an editable
  # install bakes in an absolute path outside the store, which is exactly what
  # a sandbox does not have. pyright reads first-party code through the
  # `extraPaths` in pyproject.toml, so what this env is really for is the
  # third-party stubs and the generated `nanopynix_bindings` ones.
  #
  # The spec matches the dev shell's, `docs` extra included, because
  # `extraPaths` lists `docs` and `docs/conf.py` imports sphinx.
  pythonEnv = pythonSet.mkVirtualEnv "nanopynix-check-env" {
    nanopynix = [ "test" ];
    nanopynix-helpers = [ "test" ];
    pynix = [
      "test"
      "ekn"
      "docs"
    ];
    ekn = [ ];
    pytest-agent = [ ];
  };

  mkCheck =
    name: nativeBuildInputs: command:
    runCommand "nanopynix-check-${name}" { inherit nativeBuildInputs; } ''
      # Both tools want somewhere to write a cache, and the source is a
      # read-only store path.
      export HOME="$TMPDIR"
      export RUFF_CACHE_DIR="$TMPDIR/ruff"
      cd ${source}
      ${command}
      touch "$out"
    '';
in
{
  lint = mkCheck "lint" [ ruff ] "ruff check --no-cache .";

  # The configuration that AGENTS.md says to keep at zero findings. This is
  # the sentence that makes that true.
  lint-strict = mkCheck "lint-strict" [ ruff ] "ruff check --no-cache --config ruff-strict.toml .";

  # `ruff format --check`, never `treefmt`. treefmt writes. Its exclusion of
  # the LSP fixture tree also belongs to the nix formatter alone, so this
  # command covers the same files as treefmt's python formatter, and no more.
  format = mkCheck "format" [ ruff ] "ruff format --no-cache --check .";

  types = mkCheck "types" [ pyright pythonEnv ] "pyright --pythonpath ${pythonEnv}/bin/python";

  # No drift gate here, although issue #22 asks for one. Both
  # `check_all_settings_model_drift(include_optional=True)` and
  # `check_all_store_model_drift()` already run inside the test suite
  # (tests/nanopynix/bindings/test_util.py, tests/nanopynix/test_stores.py),
  # and CI runs that suite against every supported Nix version on both
  # backends. A derivation here would check one version, so it would be
  # strictly weaker than what already runs, and slower to build than the four
  # above because it needs a working store and evaluator.
}
