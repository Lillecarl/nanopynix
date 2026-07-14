{
  lib,
  writeShellApplication,
  nanopynix-nixVersions,
}:
writeShellApplication {
  name = "nanopynix-nix-version-tests";
  text = lib.concatStringsSep "\n" (
    lib.mapAttrsToList (nixVersion: nanopynix: ''
      echo "==> Testing nanopynix against ${nixVersion}"
      ${nanopynix.passthru.tests}/bin/nanopynix-tests "$@"
    '') nanopynix-nixVersions
  );
}
