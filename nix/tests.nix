{
  lib,
  writeShellApplication,
  python,
  pytest,
  nix,
  nixpkgs,
  nanopynix,
  nanopynix-proto,
  pynix,
}:
let
  pythonDeps =
    nanopynix.dependencies
    ++ nanopynix.nativeBuildInputs
    ++ nanopynix.passthru.testInputs
    ++ nanopynix-proto.nativeBuildInputs
    ++ pynix.dependencies
    ++ [
      nanopynix
      pynix
      pytest
    ];
  pythonEnv = python.withPackages (pp: pythonDeps);
in
writeShellApplication {
  name = "nanopynix-tests";
  runtimeInputs = [
    pythonEnv
    nix
  ];
  text = ''
    cd ${lib.cleanSource ../.}
    export PYTHONNOUSERSITE=1
    export NIX_PATH=nixpkgs=${nixpkgs}
    exec python -m pytest -p no:cacheprovider tests "$@"
  '';
  passthru = { inherit pythonEnv pythonDeps; };
}
