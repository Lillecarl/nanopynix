{
  lib,
  nanopynixVersions,
  nixpkgs,
  writeShellApplication,
}:
let
  tests = lib.pipe nanopynixVersions [
    lib.attrsToList
    (lib.map (x: x.value))
    (lib.map (x: {
      nixVersion = x.nix.version;
      nanopynixVersion = x.nanopynix.version;
      command = lib.getExe x.tests;
    }))
  ];
in
writeShellApplication {
  name = "nanopynix-nix-version-tests";
  text = lib.concatLines (
    [ "export NIX_PATH=nixpkgs=${nixpkgs}" ]
    ++ lib.map (
      {
        nixVersion,
        nanopynixVersion,
        command,
      }:
      ''
        echo "==> Testing nanopynix ${nanopynixVersion} against Nix ${nixVersion}"
        ${command} "$@"
      ''
    ) tests
  );
}
