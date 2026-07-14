{
  lib,
  writeShellApplication,
  nanopynix-nixVersions,
}:
let
  uniqueNanopynixVersions = lib.foldl' (
    versions: nixVersion:
    let
      nanopynix = nanopynix-nixVersions.${nixVersion};
      outputPathHash = builtins.hashString "sha256" (toString nanopynix);
    in
    if versions ? ${outputPathHash} then
      versions
    else
      versions // {
        ${outputPathHash} = {
          inherit nixVersion nanopynix;
        };
      }
  ) { } (lib.attrNames nanopynix-nixVersions);
in
writeShellApplication {
  name = "nanopynix-nix-version-tests";
  text = lib.concatStringsSep "\n" (
    lib.mapAttrsToList (_: { nixVersion, nanopynix }: ''
      echo "==> Testing nanopynix against ${nixVersion}"
      ${nanopynix.passthru.tests}/bin/nanopynix-tests "$@"
    '') uniqueNanopynixVersions
  );
}
