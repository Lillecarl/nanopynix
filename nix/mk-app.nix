# A release build of one of this repo's applications.
#
# Not a package with an entry point, but a venv plus a thin symlink tree over
# it: `mkApplication` takes the shape of the package's own `$out` and links the
# corresponding paths out of the venv, skipping site-packages. So
# `$out/bin/<name>` is a real venv entry point that runs standalone -- which is
# also what makes generating completions possible at all.
#
# Under nixpkgs' builders completions were generated in the package's own
# `postInstall`, by running `$out/bin/<name>`. A pyproject.nix builders package
# propagates nothing, so its entry point is not runnable during its own build.
# Generating out here, against the finished application, is both the only way
# and the more honest one: it exercises the thing users get.
{
  lib,
  runCommand,
  symlinkJoin,
  installShellFiles,
  makeWrapper,
  cacert,
  pyprojectUtil,
}:

{
  # Attribute name of the project in `pythonSet`, and the name of the
  # executable it installs. These are the same for every application here; if
  # they ever diverge, that is worth a second argument rather than a guess.
  name,
  # The package set to resolve `name` in, and the package itself. Both, because
  # `mkApplication` needs the venv for content and the package for shape.
  pythonSet,
  package ? pythonSet.${name},
  # `{ var = "_EKN_COMPLETE"; }` for a click/clypi-style completion protocol,
  # or null for a program that has none.
  completions ? null,
  # Put on the program's PATH via a wrapper. For tools the program shells out
  # to at runtime rather than imports.
  pathInputs ? [ ],
  # Give the program a default CA bundle. For a program that initialises
  # OpenSSL at start-up and so cannot start at all where there is no trust
  # store -- which is every Nix build sandbox. See issue #62, and the
  # `ekn-sandbox` gate easykubenix keeps over its own CLI.
  caBundle ? false,
}:

let
  venv = pythonSet.mkVirtualEnv "${name}-env" { ${name} = [ ]; };

  app = pyprojectUtil.mkApplication { inherit venv package; };

  # `wrapped`, and not `app`: this derivation is itself a Nix build sandbox
  # running the program, so it is the same case as the easykubenix
  # derivations that shell out to `ekn`. It used to export `SSL_CERT_FILE`
  # here to survive that. It does not any more, and that is deliberate --
  # a `caBundle` wrapper that stops working fails this build, so the fix
  # for #62 verifies itself on every build of the application.
  generatedCompletions =
    runCommand "${name}-completions"
      {
        nativeBuildInputs = [
          installShellFiles
        ];
      }
      (
        lib.concatMapStrings
          (shell: ''
            installShellCompletion --cmd ${name} \
              --${shell} <(env ${completions.var}=source_${shell} ${wrapped}/bin/${name})
          '')
          [
            "bash"
            "zsh"
            "fish"
          ]
      );

  # **`--set-default SSL_CERT_FILE` does not work here, and the reason is the
  # whole difficulty.** A Nix build sandbox does not leave the variable unset.
  # It sets both `SSL_CERT_FILE` and `NIX_SSL_CERT_FILE` to
  # `/no-cert-file.crt`, a path that deliberately does not exist -- measured,
  # by reading `env` out of a plain `runCommand`. So the variable is always
  # set, `--set-default` never fires, and the program still starts with no
  # trust store.
  #
  # The condition that is true in a sandbox and false everywhere else is
  # therefore "the inherited `SSL_CERT_FILE` names nothing readable", which is
  # what this tests. A caller with a real trust store, including a private CA,
  # keeps it. `NIX_SSL_CERT_FILE` is left alone: it is Nix's own variable, and
  # nothing in a sandbox can reach the network for it to matter.
  caBundleRun = ''
    if [ ! -r "''${SSL_CERT_FILE:-}" ]; then export SSL_CERT_FILE="${cacert}/etc/ssl/certs/ca-bundle.crt"; fi
  '';

  # Two independent reasons to wrap, so they compose in one wrapper rather
  # than each claiming the only one.
  wrapperArgs =
    lib.optionals caBundle [
      "--run"
      caBundleRun
    ]
    ++ lib.optionals (pathInputs != [ ]) [
      "--prefix"
      "PATH"
      ":"
      (lib.makeBinPath pathInputs)
    ];

  # `$out/bin/${name}` alone loses nothing. `mkApplication` mirrors the
  # package's own `$out` minus `nix-support` and `site-packages`
  # (`build/util/mk-application.py`), and an installed wheel of a program has
  # nothing outside `bin/`.
  wrapped =
    if wrapperArgs == [ ] then
      app
    else
      runCommand "${name}-wrapped" { nativeBuildInputs = [ makeWrapper ]; } ''
        mkdir -p "$out/bin"
        makeWrapper "${app}/bin/${name}" "$out/bin/${name}" ${lib.escapeShellArgs wrapperArgs}
      '';
in
symlinkJoin {
  inherit name;
  paths = [ wrapped ] ++ lib.optional (completions != null) generatedCompletions;
  inherit (package) meta;
  passthru = package.passthru // {
    inherit venv package;
    inherit (package) version;
  };
}
