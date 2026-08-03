# The static gates, as derivations, so CI can fail on them.
#
# Commands that every contributor is asked to keep clean and that nothing
# enforced. `ruff-strict.toml` in particular is a large, considered
# configuration whose whole value is that it reports nothing, and until this
# existed the only thing keeping it at zero was habit.
#
# One derivation each rather than one that runs all of them, so a failing run
# names the gate that failed, and so the cheap ones still report when pyright
# is the slow one.
#
# `grpclib-transports` at the bottom is the one gate here that runs tests
# rather than a static tool, and it is here because nothing else would run
# them. That suite used to run by itself: the project came from a separate
# repository as a nixpkgs `buildPythonPackage`, so `pytestCheckHook` executed
# it inside every build. Vendoring the project moved it to pyproject.nix's
# builders, which have no check phase, and the repository's own test runner
# (nanopynix/tests.nix) collects `tests/` and not this. Without the
# derivation below, vendoring would have quietly deleted a passing test suite
# from CI.
{
  lib,
  runCommand,
  ruff,
  pyright,
  shellcheck,
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
      ../greeter-proto
      ../grpclib-transports
      ../pynix
      ../ekn
      ../pytest-agent
      ../tests
      ../tools
      ../docs
      ../ci
      # `scripts/` holds hand-written shell, which no gate read until
      # `check-shell` below. `writeShellApplication` shellchecks only the
      # scripts that it generates, and none of these is one of those.
      ../scripts
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
    # The `test` extra, because pyright reads grpclib-transports' own tests
    # and benchmarks and they import `greeter`, `asyncssh` and `rich`.
    grpclib-transports = [ "test" ];
  };

  # A second venv, holding this one library and its test extra and nothing
  # else. Not `pythonEnv` above: that one is built for pyright and carries
  # every project in the repository, so a `grpclib_transports` import
  # satisfied by some other project's dependency edge would go unnoticed --
  # which is the failure this suite exists to catch. Small enough that the
  # separate venv costs nothing.
  grpclibEnv = pythonSet.mkVirtualEnv "grpclib-transports-test-env" {
    grpclib-transports = [ "test" ];
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

  # `scripts/` was covered by nothing. `writeShellApplication` runs shellcheck
  # over the script it builds, which covers the test runner of
  # nanopynix/tests.nix and no hand-written file, so these three grew without a
  # gate. `-x` follows a `source`, and the scripts are the only shell here.
  shell = mkCheck "shell" [ shellcheck ] "shellcheck -x scripts/*.sh";

  # The vendored library's own suite. See this file's header for why it is a
  # gate here and not a check phase.
  #
  # `-p no:cacheprovider`, because the source is a read-only store path.
  # `grpclib-transports/tests/conftest.py` already knows it is in a sandbox
  # and skips the `tcp` cases there, so nothing has to be excluded by hand.
  #
  # `tests`, spelled out, and not the project directory: an explicit argument
  # replaces `testpaths`, so naming the directory would also collect
  # `benchmarks`, which is a measurement run rather than a gate -- and whose
  # `_bench_utils` writes a dump directory beside itself at import time, in
  # what is a read-only store path here.
  grpclib-transports =
    mkCheck "grpclib-transports" [ grpclibEnv ]
      "python -m pytest -p no:cacheprovider grpclib-transports/tests";

  # No drift gate here, although issue #22 asks for one. Both
  # `check_all_settings_model_drift(include_optional=True)` and
  # `check_all_store_model_drift()` already run inside the test suite
  # (tests/nanopynix/bindings/test_util.py, tests/nanopynix/test_stores.py),
  # and CI runs that suite against every supported Nix version on both
  # backends. A derivation here would check one version, so it would be
  # strictly weaker than what already runs, and slower to build than the four
  # above because it needs a working store and evaluator.
}
