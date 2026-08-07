# The development environment for this subproject.
#
# Run the suite with it, from anywhere in the repository:
#
#     nix develop --file completion-spike/shell.nix --command pytest completion-spike
#
# **The three shells are part of the environment, and so is the terminfo
# database.** The tests drive a real fish, bash and zsh on a pty, so a shell
# that is missing is a test that cannot run, and `TERM=xterm` with no terminfo
# is a fish that draws no candidate list at all.
#
# `cyclopts` and `pexpect` are not in the repository's own dev shell venv --
# this subproject is a nixpkgs `buildPythonApplication` rather than one of the
# pyproject.nix builders projects, so its dependencies are resolved here and
# in nix/completion-spike.nix, and nowhere else.
let
  repo = import ../. { };
  inherit (repo) pkgs;

  python = pkgs.python3.withPackages (
    ps: with ps; [
      cyclopts
      pexpect
      pytest
    ]
  );
in
pkgs.mkShellNoCC {
  name = "completion-spike-shell";

  packages = [
    python
    pkgs.fish
    pkgs.bashInteractive
    pkgs.zsh
    pkgs.ncurses
  ];

  # Set as derivation attributes rather than in a `shellHook`, so that they
  # apply to `nix develop --command` and not only to an interactive shell.
  #
  # `src` on the path, and no install step: an edit is live, which is what a
  # spike needs. The Nix build tests the installed program instead, and
  # tests/conftest.py takes whichever of the two it finds.
  PYTHONPATH = toString ./src;
  TERMINFO_DIRS = "${pkgs.ncurses}/share/terminfo";
}
