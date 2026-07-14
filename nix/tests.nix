{
  lib,
  writeShellApplication,
  python,
  pytest,
  nix,
  nixpkgs,
  nanopynix,
  pynix,
}:
let
  pythonEnv = python.withPackages (
    pp:
    nanopynix.dependencies
    ++ nanopynix.nativeBuildInputs
    ++ nanopynix.passthru.testInputs
    ++ pynix.dependencies
    ++ [
      nanopynix
      pynix
      pytest
    ]
  );
  testSource = lib.cleanSource ../.;
in
writeShellApplication {
  name = "nanopynix-tests";
  runtimeInputs = [
    pythonEnv
    nix
  ];
  text = ''
    cd ${testSource}
    export PYTHONNOUSERSITE=1
    export NIX_PATH=nixpkgs=${nixpkgs}
    python -m pytest -p no:cacheprovider tests "$@"
  '';
}
