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
}:

let
  venv = pythonSet.mkVirtualEnv "${name}-env" { ${name} = [ ]; };

  app = pyprojectUtil.mkApplication { inherit venv package; };

  generatedCompletions =
    runCommand "${name}-completions"
      {
        nativeBuildInputs = [
          installShellFiles
          cacert
        ];
      }
      (
        ''
          # ekn imports pygit2 at start-up, which initialises OpenSSL and
          # fails outright without a CA bundle -- even though generating
          # completions touches no network.
          export SSL_CERT_FILE="${cacert}/etc/ssl/certs/ca-bundle.crt"
          export GIT_SSL_CAINFO="$SSL_CERT_FILE"
        ''
        +
          lib.concatMapStrings
            (shell: ''
              installShellCompletion --cmd ${name} \
                --${shell} <(env ${completions.var}=source_${shell} ${app}/bin/${name})
            '')
            [
              "bash"
              "zsh"
              "fish"
            ]
      );

  wrapped =
    if pathInputs == [ ] then
      app
    else
      runCommand "${name}-wrapped" { nativeBuildInputs = [ makeWrapper ]; } ''
        mkdir -p "$out/bin"
        makeWrapper "${app}/bin/${name}" "$out/bin/${name}" \
          --prefix PATH : ${lib.makeBinPath pathInputs}
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
