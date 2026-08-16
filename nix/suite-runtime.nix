# What the test suite needs on PATH, wherever it runs.
#
# **Two places run this suite, and they drifted apart twice.** The packaged
# runner of `nanopynix/tests.nix` is what CI executes; the dev shell of
# `nix/shell.nix` is what a person executes. A tool that only one of them
# carried made the same failure both times: the suite passed interactively and
# failed in CI, and the difference read as a defect in the code under test
# rather than as a gap in one PATH.
#
# - `tofuCoreSchemaTool` was the first. `get_core_schema` catches the `OSError`
#   and returns None, so the LSP answered "no schema" instead of erroring, and
#   the scenarios read as a schema bug.
# - `bashInteractive` was the second. Three `test_develop` tests ran the
#   restored environment of a derivation under whatever `bash` the host had,
#   and macOS ships 3.2.57, which cannot parse the `;&` that stdenv writes.
#
# So the list lives here and both take it whole. Add a runtime dependency of
# the suite here, and neither consumer can forget it.
#
# **Only what the suite needs at run time belongs here.** The development
# tooling -- ruff, pyright, treefmt and the rest -- is the dev shell's own
# business, and the runner must not carry it. The Python environment is not
# here either, because the two differ on purpose: the runner takes the
# non-editable `nanopynix-test-env`, and the shell takes the editable one.
{
  bashInteractive,
  coreutils,
  gdb,
  gitMinimal,
  nix-cli,
  tofuCoreSchemaTool,
  storeExecTools,
}:
[
  # **The Nix of this scope, which is the Nix the bindings link.**
  # `test_flake_metadata` runs `nix flake metadata` and `test_develop` runs
  # `nix print-dev-env`, both as oracles, and both read the binary from PATH.
  # A machine whose own Nix is newer makes them compare two Nix releases
  # rather than pynix: 2.35.1 against the 2.34.8 of these bindings reports a
  # different flake `fingerprint`. `pynix/tests/support/nix_oracle.py` checks
  # and skips, because a shell is not the only way to run the suite.
  nix-cli

  # **The bash of this closure, because the host's may not read what the suite
  # writes.** `pynix develop` restores an environment that carries bash
  # functions of stdenv, and one of them uses `;&`, the fallthrough form of a
  # `case` arm, which bash added in 4.0. macOS ships 3.2.57, because every
  # release after it is GPLv3. Measured with the construct from the line that
  # failed: 3.2 gives a syntax error, 5.3 runs it.
  #
  # This does not repair `pynix develop` for a user whose own bash is 3.2.
  # Issue #152 holds that.
  bashInteractive

  # mktemp/wc/head/rm, for bounding the post-mortem backtrace of the runner.
  # `writeShellApplication` only *prepends* its inputs to the ambient PATH, so
  # without this the crash path would silently depend on the host having
  # coreutils -- true on a GitHub runner, not something this suite should rely
  # on.
  coreutils

  # The post-mortem backtrace itself.
  gdb

  # Nix runs `git` off PATH to fetch a `git+file:` or a dirty `path:` flake
  # input, so `nanopynix/tests/bindings/test_flake.py` needs one. The host git
  # worked until the ASAN job, which sets LD_PRELOAD to the sanitizer runtime.
  # That runtime links against the glibc of this closure, and the loader then
  # gives the host git a mix of two glibcs, so
  # `test_eval_flake_writes_lock_file` failed.
  #
  # **Minimal, because the other half of git is perl.** `pkgs.git` carries a
  # 384.2 MiB closure over 87 store paths, 40 of them perl. `pkgs.gitMinimal`
  # carries 159.3 MiB over 34 paths and none of them is perl. What it drops is
  # `git svn`, `git send-email`, `git cvs*`, gitweb and the gui, and Nix calls
  # none of them: the only subcommand its fetcher names is `symbolic-ref`.
  gitMinimal

  # `pynix._lsp._tofu_core_schema` resolves `nanopynix-tofu-core-schema` off
  # PATH, so every core (non-provider) meta-argument hover and completion needs
  # it present. The released `pynix` app supplies it through its wrapper; these
  # two supply it here.
  tofuCoreSchemaTool
]
# `nanopynix.store_exec_prefix` resolves this off PATH. Every Nix session in
# the suite runs against a *relocated* store unless
# `NANOPYNIX_TEST_SYSTEM_STORE` says otherwise, so without it the terranix LSP
# scenarios cannot exec `tofu` at all.
#
# Empty off Linux, where the tool does not exist. `default.nix` says why, and
# the tests that need it carry a marker.
++ storeExecTools
