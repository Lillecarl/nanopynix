{
  lib,
  nanopynix-nixVersions,
  nixpkgs,
  writeShellApplication,
  gitMinimal,
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
      versions
      // {
        ${outputPathHash} = {
          inherit nixVersion nanopynix;
        };
      }
  ) { } (lib.attrNames nanopynix-nixVersions);
in
writeShellApplication {
  name = "nanopynix-nix-version-tests";
  runtimeInputs = [
    gitMinimal
  ];
  text = lib.concatLines (
    [ "export NIX_PATH=nixpkgs=${nixpkgs}" ]
    ++ lib.mapAttrsToList (_: { nixVersion, nanopynix }: ''
      echo "==> Testing nanopynix against ${nixVersion}"
      ${nanopynix.passthru.tests}/bin/nanopynix-tests "$@"
    '') uniqueNanopynixVersions
  );
}
