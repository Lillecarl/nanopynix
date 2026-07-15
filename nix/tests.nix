{
  lib,
  writeShellApplication,
  python,
  nix,
  nixpkgs,
  pynix,
}:
let
  pythonEnv = python.withPackages (
    _: pynix.dependencies ++ pynix.optional-dependencies.test ++ [ pynix ]
  );
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
    exec python -m pytest -p no:cacheprovider "$@"
  '';
  passthru = { inherit pythonEnv; };
}
