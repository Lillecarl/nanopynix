{
  lib,
  buildPythonApplication,
  cacert,
  installShellFiles,
  python,
  renderPyproject,
}:
let
  attrs = renderPyproject {
    projectRoot = toString ./.;
    inherit python;
  };
in
buildPythonApplication (
  attrs
  // {
    src = ./.;

    nativeBuildInputs = (attrs.nativeBuildInputs or [ ]) ++ [
      cacert
      installShellFiles
    ];

    postInstall = ''
      export GIT_SSL_CAINFO="${cacert}/etc/ssl/certs/ca-bundle.crt"
      installShellCompletion --name ekn --bash <(env _EKN_COMPLETE=source_bash $out/bin/ekn)
      installShellCompletion --name ekn --zsh  <(env _EKN_COMPLETE=source_zsh  $out/bin/ekn)
      installShellCompletion --name ekn --fish <(env _EKN_COMPLETE=source_fish $out/bin/ekn)
    '';

    meta = attrs.meta // {
      platforms = lib.platforms.unix;
    };
  }
)
