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
# `grpclib-transports` and `pytest-agent` are the two gates here that run
# tests rather than a static tool, and both are here because nothing else
# would run them. Each subproject carries its own pytest configuration, and
# the repository's own test runner (nanopynix/tests.nix) collects what
# `testpaths` in the repository `pytest.ini` names, so a suite that nobody
# names runs nowhere.
#
# The two arrived at that state differently. `grpclib-transports` used to run
# by itself: the project came from a separate repository as a nixpkgs
# `buildPythonPackage`, so `pytestCheckHook` executed it inside every build,
# and vendoring it moved the project to pyproject.nix's builders, which have
# no check phase. `pytest-agent` never ran anywhere at all -- it is the
# plugin that every other suite here reports through, so a defect in it
# reaches every job while its own 14 test modules gate nothing.
{
  lib,
  runCommand,
  ruff,
  pyright,
  shellcheck,
  pythonSet,
  nixos,
  pynix,
  pynixd,
  bashInteractive,
  fish,
  zsh,
  ncurses,
  nix,
  completionSpike,
}:
let
  # Only the trees the gates read. An allowlist, and not the shared denylist
  # of `nix/source.nix` that the test runner and the docs build take: a lint
  # gate must not report on a file that is gitignored and absent under a flake
  # evaluation, and naming each input keeps an unrelated edit from rebuilding
  # all four gates.
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
      # The CLI layer, which issue #222 moved out of `../pynix`. Without it
      # `check-lint`, `check-format` and `check-types` read a smaller tree
      # than `ruff check .` in the dev shell reads.
      ../libpynix
      ../greeter-proto
      ../grpclib-transports
      # The whole tree, and not the one subproject that `checks.nix-daemon-protocol`
      # reads. `ruff.toml` stopped excluding `pynixd`, so `check-lint` and
      # `check-format` report on it, and a gate that reads less than a
      # developer's own `ruff check .` gives two answers to one question.
      #
      # An edit to any part of pynixd rebuilds all of them. Issue #131 took
      # 320 KiB of that back: `todo/`, `research/`, `ai/` and the rest of the
      # agent-workflow trees left the repository. The open work in them is
      # issues #133 to #137, and the reference material moved to
      # `pynixd/docs/notes/`.
      ../pynixd
      ../pynix
      # The language server, which issue #107 moved out of `../pynix`. Without
      # it `check-lint`, `check-format` and `check-types` read a smaller tree
      # than `ruff check .` in the dev shell reads.
      ../pynix-lsp
      ../completion-spike
      ../pytest-agent
      ../test-support
      ../nanopynix-testing
      ../tests
      ../tools
      ../docs
      ../ci
      # `scripts/` holds hand-written shell, which no gate read until
      # `check-shell` below. `writeShellApplication` shellchecks only the
      # scripts that it generates, and none of these is one of those.
      ../scripts
      # The licence step of the wheel build. It runs inside a derivation, so
      # no test imports it and only these gates read it. It is also the check
      # that stops an unattributed library reaching PyPI, so a fault in it is
      # a licence fault.
      ../nix/wheel-notice.py
      # The gate step of the same build, and here for the same reason. A
      # fault in it is a gate that passes, which is worse than no gate.
      ../nix/wheel-gates.py
      # The renderer of the completion scripts. `nix/mk-app.nix` runs it inside
      # each application build, and `checks.completions` runs it as well: the
      # dev-shell branch of its `scripts` fixture renders the three scripts
      # rather than reading them out of a store path, and
      # `test_the_renderer_writes_where_it_is_told` states the arguments it
      # takes. Absent from this list the file is not in the source, and Python
      # answers a missing script with exit status 2 and no message at all.
      #
      # It also brings the file into `check-lint` and `check-format`, which
      # read three of the four `nix/*.py` files without it.
      ../nix/render-completions.py
      # The rewrite that lowers the glibc floor of every object of the wheel
      # closure. It runs as a setup hook inside each build, so no test imports
      # it either, and a fault in it is a wheel that installs and then fails to
      # load on the oldest host it claims.
      ../nix/lower-glibc.py
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
    # pyright reads `libpynix/src` and `libpynix/tests`, and that suite
    # imports `argcomplete` and `pytest` directly.
    libpynix = [ "test" ];
    pynix = [
      "test"
      "docs"
    ];
    # The language server, which issue #107 moved out of `pynix`. pyright
    # reads `pynix-lsp/src` and `pynix-lsp/tests`, so the env has to carry
    # `pygls`, `lsprotocol`, `jsonschema` and `pytest-lsp`.
    pynix-lsp = [ "test" ];
    pytest-agent = [ ];
    # The `test` extra, because pyright reads grpclib-transports' own tests
    # and benchmarks and they import `greeter`, `asyncssh` and `rich`.
    grpclib-transports = [ "test" ];
    test-support = [ "test" ];
    nanopynix-testing = [ ];
    # `completion-spike` itself is not a package of this set -- it is a nixpkgs
    # `buildPythonApplication`. Its two dependencies are named directly, so
    # that pyright can read its source and its tests. Without them pyright
    # reported 56 errors on that tree, every one of them an unknown type
    # reached through `cyclopts` or `pexpect`.
    cyclopts = [ ];
    pexpect = [ ];
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

  # And a third, for the same reason. This one must also be an installed
  # `pytest-agent` rather than a `pythonpath` entry: the suite asks
  # `plugin_registered_via_entry_points()` to decide whether to pass `-p
  # pytest_agent.plugin` to each inner pytest, and that question only has the
  # right answer when the distribution metadata is really there.
  pytestAgentEnv = pythonSet.mkVirtualEnv "pytest-agent-test-env" {
    pytest-agent = [ "test" ];
  };

  # And a fourth. `test-support` holds the helpers that every suite here shares
  # and that name no Nix concept, so its own suite must not reach anything that
  # does: a venv with nanopynix in it would let a helper grow a dependency on
  # the library and nothing would report it. That is the rule this project
  # exists to keep, so the venv is what enforces it.
  testSupportEnv = pythonSet.mkVirtualEnv "test-support-test-env" {
    test-support = [ "test" ];
  };

  # And a fifth. `nanopynix-helpers` carries its own suite since issue #130.
  # The repository run reaches it through `testpaths`, and this gate is what
  # proves the suite also stands up alone, under its own rootdir and with
  # only this project's dependencies. Its `test` extra reaches
  # `nanopynix[test]`, which supplies pytest, `test-support` and
  # `nanopynix-testing`.
  helpersEnv = pythonSet.mkVirtualEnv "nanopynix-helpers-test-env" {
    nanopynix-helpers = [ "test" ];
  };

  # And a sixth. `libpynix` is the command-line layer that issue #222 moved
  # out of `pynix`. A venv holding this project alone is what proves it stands
  # up without `pynix`, and without any part of Nix: the whole reason it is a
  # separate project is that a consumer should be able to take the parser
  # without the evaluator.
  libpynixEnv = pythonSet.mkVirtualEnv "libpynix-test-env" {
    libpynix = [ "test" ];
  };

  # And a seventh. `nix-daemon-protocol` is the wire package under `pynixd/`,
  # and its suite is the pure half of what pynixd tests: no daemon, no Nix
  # binary and no SSH, so it is the suite of that project that a sandbox can
  # run. A venv holding this project alone is what proves the package stands
  # up without pynixd, which is the reason it is a separate distribution.
  protocolEnv = pythonSet.mkVirtualEnv "nix-daemon-protocol-test-env" {
    nix-daemon-protocol = [ "test" ];
  };

  # And an eighth, for pynixd itself. Only its unit suite runs here. The
  # functional suite drives a real `nix`, a real daemon and a real SSH
  # session, and a build sandbox offers none of the three, so a gate that ran
  # it would report the sandbox rather than the code.
  pynixdEnv = pythonSet.mkVirtualEnv "pynixd-test-env" {
    pynixd = [ "test" ];
  };

  # And a ninth. `pynix` and nothing else, which is what the `pynix-isolated`
  # gate below asks a question of. The dev shell carries the language server
  # on purpose, so no environment that a person works in can answer it.
  pynixOnlyEnv = pythonSet.mkVirtualEnv "pynix-only-env" {
    pynix = [ ];
  };

  # And a tenth, for the completion suite of `pynix`. `test-support` carries
  # `shell_pty`, which drives a shell on a pty, and its `test` extra carries
  # pytest. **`pynix` is deliberately not in it**: that suite completes against
  # the *installed* application, which the gate names by its store path, and a
  # second `pynix` on the search path would make the answer ambiguous.
  #
  # `libpynix` is in it for `argcomplete` alone. `nix/render-completions.py`
  # imports `argcomplete.shell_integration`, and
  # `test_the_renderer_writes_where_it_is_told` runs that script with this
  # interpreter. `libpynix` names `argcomplete` and names nothing else, and it
  # is not a second `pynix`, so the ambiguity above stays avoided.
  completionsEnv = pythonSet.mkVirtualEnv "pynix-completions-test-env" {
    test-support = [ "test" ];
    libpynix = [ ];
  };

  # A minimum NixOS configuration, so that the module of pynixd is evaluated.
  # `nixos` needs a boot loader, a root filesystem and a state version before
  # it evaluates anything at all, and none of the three says anything about
  # pynixd.
  evalModule =
    settings:
    (nixos {
      # `settings` goes in `imports`, and is not merged with `//`. The
      # operator replaces a whole key, so `{ services.pynixd.package = ...; }
      # // { services.pynixd.enable = true; }` loses the package.
      imports = [
        ../pynixd/nix/nixos
        settings
      ];
      boot.loader.grub.enable = false;
      fileSystems."/" = {
        device = "/dev/null";
        fsType = "ext4";
      };
      system.stateVersion = "25.05";
      services.pynixd.package = pynixd;
    }).config;

  enabled = evalModule { services.pynixd.enable = true; };
  disabled = evalModule { };

  # The module with one setting overridden, so the gate can see that the
  # defaults are defaults. `settingsDefaults` in `pynixd/nix/common.nix` is
  # wrapped in `mkDefault`; without that wrapper these are values, and a user
  # who sets `unix_path` gets an evaluation conflict rather than an override.
  # The wrapper is one `lib.mapAttrsRecursive` and is the easiest thing to
  # lose when that block moves between files, which is why it is checked here
  # and not only read.
  overridden = evalModule {
    services.pynixd.enable = true;
    services.pynixd.settings.unix_path = "/run/pynixd/custom.sock";
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
  #
  # The wheel closure needed a compiler wrapper of its own until it moved back
  # onto the gcc stdenv of nixpkgs, and that wrapper was the second command
  # here. It is gone, and so is the exception it needed.
  shell = mkCheck "shell" [
    shellcheck
  ] "shellcheck -x scripts/*.sh";

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
  grpclib-transports = mkCheck "grpclib-transports" [
    grpclibEnv
  ] "python -m pytest -p no:cacheprovider grpclib-transports/tests";

  # The wire protocol package that arrived with pynixd, gated for the same
  # reason as the gate above it: nothing else runs this suite. `testpaths` in
  # the repository `pytest.ini` names no directory of `pynixd/`, and the
  # packaged runner reads that file.
  #
  # `tests`, spelled out, for the same reason as the gate above it: an
  # explicit argument replaces `testpaths`, and the project also carries a
  # `benchmarks` directory that measures rather than gates.
  nix-daemon-protocol = mkCheck "nix-daemon-protocol" [
    protocolEnv
  ] "python -m pytest -p no:cacheprovider pynixd/nix-daemon-protocol/tests";

  # The unit suite of pynixd, and only the unit suite. See `pynixdEnv` for why
  # the functional half stays out.
  #
  # The directory of the project, and not `pynixd/tests/unit`, would collect
  # the functional suite through `testpaths`, so the argument is the directory
  # that this gate runs.
  pynixd = mkCheck "pynixd" [
    pynixdEnv
  ] "cd pynixd && python -m pytest -p no:cacheprovider tests/unit";

  # **`pynix` alone, and the language server must not arrive with it.** Issue
  # #107 split `pynix-lsp` out to take `pygls`, `lsprotocol` and `jsonschema`
  # off the start-up of `pynix build`: 349 of the 966 modules that `import
  # pynix` loaded came from those three, and the split removed 62 of the 966.
  #
  # A test in either suite cannot state this. The dev shell installs both
  # projects, because a developer here runs the server as well as the CLI. So
  # the question "is the server absent" only has an answer inside a venv built
  # for the question, which is what `pynixOnlyEnv` is.
  #
  # **The gate asks two questions, and `jsonschema` is why there are two.**
  # `pygls`, `lsprotocol` and `pynix_lsp` must not be installed at all: no
  # other project here needs them. `jsonschema` is a dependency of
  # `nanopynix`, which ships `jsonschema_primops()`, so it is installed and
  # correct -- what must not happen is `import pynix` loading it. The second
  # question is therefore about `sys.modules` after the import, which is also
  # the cost that issue #107 measured. Issue #123 tracks that cost.
  #
  # There is no third question about a subcommand any more. `pynix` mounted
  # `Lsp` through an optional import until issue #123, so this gate also had to
  # state that the mount did not happen here. `pynix-lsp` is the program an
  # editor calls, and it sits beside `pynix` on the PATH of the dev shell, so
  # the alias is gone and the two questions above are the whole gate.
  #
  # `runCommand` and not `mkCheck`: this gate reads no file of the source
  # tree, and `cd`-ing into one would put a `pynix/` directory on `sys.path`
  # as a namespace portion for no reason.
  pynix-isolated =
    runCommand "nanopynix-check-pynix-isolated" { nativeBuildInputs = [ pynixOnlyEnv ]; }
      ''
        python - <<'EOF'
        import importlib.util
        import sys

        NOT_INSTALLED = ("pygls", "lsprotocol", "pynix_lsp")
        NOT_IMPORTED = (*NOT_INSTALLED, "jsonschema")

        present = [n for n in NOT_INSTALLED if importlib.util.find_spec(n)]
        if present:
            raise SystemExit(f"the venv of pynix must not hold the language server, but it holds: {present}")

        import pynix

        loaded = sorted({m.split(".")[0] for m in sys.modules} & set(NOT_IMPORTED))
        if loaded:
            raise SystemExit(f"`import pynix` must not load these, and it loaded: {loaded}")

        names = {sub.cli_name or sub.__name__ for sub in pynix.Pynix.subcommands}
        print(f"pynix alone: {len(sys.modules)} modules, {len(names)} subcommands, no language server")
        EOF
        touch "$out"
      '';

  # The NixOS module of pynixd, evaluated. It is the only module this
  # repository ships, and `flake.nix` exposes it as `nixosModules.pynixd`.
  # Nothing else reads it, so without this gate a rename of an option or a
  # type error would first be seen by a person rebuilding their system.
  #
  # The second half is a regression test. `environment.systemPackages` used to
  # sit in a second element of a `mkMerge`, outside the `mkIf cfg.enable`, so
  # importing the module installed pynixd whether or not the service was
  # enabled. A module that does something when it is disabled cannot be
  # imported and left alone.
  nixos-module =
    let
      unit = enabled.systemd.services.pynixd.serviceConfig;
      installedWhenEnabled = builtins.elem pynixd enabled.environment.systemPackages;
      installedWhenDisabled = builtins.elem pynixd disabled.environment.systemPackages;
    in
    assert unit.ExecStart == "${lib.getExe pynixd} daemon";
    assert enabled.environment.etc."pynixd/pynixd.json".source != null;
    assert installedWhenEnabled;
    assert !installedWhenDisabled;
    # The defaults are defaults: an override wins, and the settings it did not
    # name survive. See `overridden` above for why this is worth a gate.
    assert enabled.services.pynixd.settings.unix_path == "/run/pynixd/pynixd.sock";
    assert overridden.services.pynixd.settings.unix_path == "/run/pynixd/custom.sock";
    assert overridden.services.pynixd.settings.ssh_port == null;
    runCommand "nanopynix-check-nixos-module" { } ''
      echo "services.pynixd evaluates, overrides cleanly, and installs nothing while disabled" > "$out"
    '';

  # The plugin every other suite in this repository reports through, gated for
  # the first time. See this file's header.
  #
  # `tests`, spelled out, for the same reason as the gate above it.
  #
  # `-p no:cacheprovider` for the read-only store path, and
  # `PYTEST_AGENT_NO_AUTODETECT=1` because the plugin activates itself when it
  # finds an agent-harness variable in the environment. Without it, this suite
  # would run in agent mode when a developer builds the gate from a Claude
  # Code session and in plain mode in CI -- two different runs from one
  # command. The inner sessions that the tests start are unaffected: they get
  # a cleaned environment from the suite's own autouse fixture.
  pytest-agent = mkCheck "pytest-agent" [ pytestAgentEnv ] ''
    export PYTEST_AGENT_NO_AUTODETECT=1
    python -m pytest -p no:cacheprovider pytest-agent/tests
  '';

  # The shared helpers, gated for the same reason as the two gates above: the
  # repository suite collects `tests/` and not this directory, so without this
  # the suite of the project that every other suite depends on would run
  # nowhere.
  #
  # The suite of the helpers package, which moved beside the package that it
  # tests. The directory and not `nanopynix-helpers/tests`, for the same
  # reason as the gate below: this project carries its own `pytest.ini`.
  nanopynix-helpers = mkCheck "nanopynix-helpers" [
    helpersEnv
  ] "python -m pytest -p no:cacheprovider nanopynix-helpers";

  # The CLI layer of issue #222. The directory and not `libpynix/tests`, for
  # the same reason as the gate above: this project carries its own
  # `pytest.ini`, and pointing pytest at the project makes `testpaths` apply.
  libpynix = mkCheck "libpynix" [
    libpynixEnv
  ] "python -m pytest -p no:cacheprovider libpynix";

  # The directory, and not `test-support/tests`. This project carries its own
  # `pytest.ini`, so pointing pytest at the project makes `testpaths` and
  # `anyio_mode` apply. Read that file: without `anyio_mode`, every async test
  # here fails rather than skips.
  #
  # `-p no:cacheprovider` for the read-only store path, as above.
  test-support = mkCheck "test-support" [
    testSupportEnv
  ] "python -m pytest -p no:cacheprovider test-support";

  # The completion spike, whose suite drives fish, bash and zsh on a pty. Named
  # here so that the `static-checks` job builds it: that job takes its list
  # from this attribute set (ci/workflows/lib.nix), and a package that no job
  # names is a suite that never runs.
  #
  # An alias, and not a `mkCheck`. The suite is the check phase of the package
  # itself, so building the package *is* running the gate. See
  # nix/completion-spike.nix for why that shape was chosen.
  # **The shell completions `pynix` installs, driven in the three shells that
  # load them.**
  #
  # Two questions, and `pynix/completions/tests/` holds both. The first is what
  # each installed file *is*: a program asked for a completion script the click
  # way prints its help screen and exits 0, and a file holding an ANSI-coloured
  # help screen sits at a path the shell loads and reports nothing. Issue #105
  # measured exactly that on a sibling program.
  #
  # The second is what each shell then *offers*, for every line a user can
  # type. This gate asked one line, `pynix bu`, and answered it in a shell
  # probe written in this file. Issue #213 measured the other eight: four are
  # wrong, and one of the four puts a command the user did not ask for on the
  # command line. A case table replaces the probe, and each broken row is
  # `xfail(strict=True)` against #105, so the fix turns the row green and the
  # run red until someone removes the mark.
  #
  # `PYNIX_INSTALLED_PREFIX` names the built application, so the suite reads
  # `share/` out of the thing a user installs. Without it the suite renders the
  # same three scripts itself, which is what a run in the dev shell does.
  #
  # `bashInteractive` and not `bash`: the driver spawns an interactive shell
  # and sends `bind`, which the non-interactive build has no readline for.
  # `ncurses` carries the terminfo database, and the driver asks for an `xterm`
  # terminal, because fish draws no candidate list at all on a terminal it
  # believes cannot address the cursor.
  #
  # `SHELL` is deliberately absent from this sandbox. clypi resolved a
  # completion through the user's login shell and raised when it did not know
  # it, so each generated script had to name its own shell; argcomplete asks
  # the shell that is running the script and reads no such variable. Issue
  # #214. Keeping the variable unset here is what would catch a return to
  # anything that needs it.
  completions = mkCheck "completions" [
    completionsEnv
    bashInteractive
    fish
    zsh
    ncurses
    nix
  ] ''
    unset SHELL
    export PYNIX_INSTALLED_PREFIX="${pynix}"
    export TERMINFO_DIRS="${ncurses}/share/terminfo"
    # **`nix` is here as a baseline, and not as a tool.**
    # `tests/test_nix_equivalence.py` asks `nix` what it would offer, through
    # `NIX_GET_COMPLETIONS`, and asserts that `pynix` offers the same. So the
    # expectation is a running program rather than a table this repository
    # wrote, and it moves when Nix moves.
    #
    # Both programs need a store before they can evaluate, and the sandbox has
    # no daemon to talk to. `NIX_REMOTE` points each of them at a local store
    # under the build directory, which is writable and empty.
    export NIX_REMOTE="$TMPDIR/completion-store"
    # `-p no:cacheprovider`, because the source is a read-only store path.
    cd pynix/completions
    pytest . -p no:cacheprovider
  '';

  completion-spike = completionSpike;

  # **No gate for the pynix suite, and that is not an oversight.** Issue #130
  # moved it to `pynix/tests/`, and the two gates above exist because a moved
  # suite would otherwise run nowhere. That suite is different: it drives a
  # real store, a real evaluator and `nix-build`, so it runs in the CI matrix
  # against every supported Nix version and both backends. The repository
  # `pytest.ini` names it in `testpaths`, which is what the packaged runner
  # reads. A derivation here would check one version inside a sandbox with no
  # network, so it would be strictly weaker than what already runs.
  #
  # No drift gate here either, although issue #22 asks for one. Both
  # `check_all_settings_model_drift(include_optional=True)` and
  # `check_all_store_model_drift()` already run inside the test suite
  # (nanopynix/tests/bindings/test_util.py, nanopynix/tests/test_stores.py),
  # and CI runs that suite against every supported Nix version on both
  # backends. A derivation here would check one version, so it would be
  # strictly weaker than what already runs, and slower to build than the four
  # above because it needs a working store and evaluator.
}
