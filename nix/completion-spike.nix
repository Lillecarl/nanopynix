# The completion spike, and its own suite.
#
# **A nixpkgs `buildPythonApplication`, and not a pyproject.nix builders
# package.** The difference is the check phase. `pytestCheckHook` runs the
# suite inside this build, so the completion behaviour of three shells is
# gated by building the package. A builders package has no check phase, which
# is why `grpclib-transports` and `pytest-agent` each need a separate
# derivation in nix/checks.nix to run at all.
#
# Nothing depends on this package, so being an application costs nothing here.
# `nix/python-set.nix` records the reason that matters: `withPackages` drops an
# application together with everything it propagates, so a *library* that
# `pynix` depended on could not take this shape.
{
  lib,
  callPackage,
  buildPythonApplication,
  hatchling,
  cyclopts,
  pytestCheckHook,
  pexpect,
  bashInteractive,
  fish,
  zsh,
  ncurses,
}:

let
  testSupport = callPackage ./test-support.nix { };
in

buildPythonApplication {
  pname = "completion-spike";
  version = "0.1.0";
  pyproject = true;

  # This subproject only. Not `nix/source.nix`, which is the whole repository:
  # nothing outside this directory is read, and a wider source would rebuild
  # the suite whenever any other file changed.
  src = lib.fileset.toSource {
    root = ../completion-spike;
    fileset = lib.fileset.unions [
      ../completion-spike/pyproject.toml
      ../completion-spike/src
      ../completion-spike/tests
    ];
  };

  build-system = [ hatchling ];
  dependencies = [ cyclopts ];

  # The three shells are the subject of the tests, so they are inputs of the
  # build and not tools that happen to be on the PATH of a developer.
  # `ncurses` carries the terminfo database: the driver asks for a `xterm`
  # terminal, because fish draws no candidate list at all on a terminal it
  # believes cannot address the cursor.
  #
  # `testSupport` carries `test_support.shell_pty`, which drives a shell on a
  # pty. That module used to be `completion_spike._pty`; issue #213 moved it,
  # because `pynix/completions/tests/` drives the same three shells against the
  # installed `pynix`. nix/test-support.nix says why that one project is built
  # the nixpkgs way as well.
  nativeCheckInputs = [
    pytestCheckHook
    testSupport
    pexpect
    bashInteractive
    fish
    zsh
    ncurses
  ];

  # `pytestCheckHook` runs after the install, and `$out/bin` is on no PATH.
  # The suite completes the real console script when it finds one, and falls
  # back to a shim built from the checkout when it does not -- so without this
  # line the build would quietly test the shim instead of what it installed.
  preCheck = ''
    export PATH="$out/bin:$PATH"
    export TERMINFO_DIRS="${ncurses}/share/terminfo"
  '';

  meta = {
    description = "Static and dynamic shell completion for a cyclopts program";
    mainProgram = "demo";
    license = lib.licenses.mit;
    maintainers = [ lib.maintainers.lillecarl ];
  };
}
