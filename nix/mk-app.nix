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
  # True for a program that answers a shell completion through argcomplete, and
  # false for one that gets no completion scripts. `nix/render-completions.py`
  # says where the script comes from.
  completions ? false,
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

  # **Rendered by argcomplete, from the name of the program.** The script is
  # the same for every argcomplete program: it exports the variables the
  # protocol names and calls the program back on file descriptor 8. Nothing is
  # read out of the command tree, so nothing here has to import the program.
  #
  # The venv's own interpreter runs it, because that is where argcomplete is.
  generatedCompletions =
    runCommand "${name}-completions"
      {
        nativeBuildInputs = [
          installShellFiles
        ];
      }
      ''
        ${venv}/bin/python ${./render-completions.py} ${name} "$PWD/rendered"
        installShellCompletion --cmd ${name} \
          --bash rendered/bash \
          --zsh rendered/zsh \
          --fish rendered/fish
      '';

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

  # **The wrapper shadows one file of the application, and carries the rest.**
  # It used to be a `runCommand` that created `$out/bin/${name}` and nothing
  # else, which was true to what the application held at the time -- an
  # installed wheel of a program has nothing outside `bin/`. It also silently
  # decided that it never would: a completion script under
  # `share/bash-completion/`, a manual page, an icon, anything a shell or a
  # desktop reads by convention when the package is in `environment.systemPackages`
  # or `home.packages`, would have been dropped here with no error.
  #
  # `symlinkJoin` over the application, with `makeWrapper` replacing the one
  # entry point, keeps every other path. `rm` first, because the join already
  # linked the unwrapped program to that name.
  wrapped =
    if wrapperArgs == [ ] then
      app
    else
      symlinkJoin {
        name = "${name}-wrapped";
        paths = [ app ];
        nativeBuildInputs = [ makeWrapper ];
        postBuild = ''
          rm "$out/bin/${name}"
          makeWrapper "${app}/bin/${name}" "$out/bin/${name}" ${lib.escapeShellArgs wrapperArgs}
        '';
      };
in
symlinkJoin {
  inherit name;
  paths = [ wrapped ] ++ lib.optional completions generatedCompletions;
  inherit (package) meta;
  passthru = package.passthru // {
    inherit venv package;
    inherit (package) version;
  };
}
