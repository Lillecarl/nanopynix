{
  lib,
  writeShellApplication,
  python,
  pytest,
  pytest-dependency,
  anyio,
  clypi,
  rich,
  structlog,
  coreutils,
  git,
  nix,
  flakeCompatishSrc,
  grpclibTransportsSrc,
  nanopynix,
  pynix,
}:
let
  pythonEnv = python.withPackages (_: [
    nanopynix
    pynix
    pytest
    pytest-dependency
    anyio
    clypi
    rich
    structlog
  ]);
  testSource = lib.cleanSource ../.;
in
writeShellApplication {
  name = "nanopynix-tests";
  runtimeInputs = [
    pythonEnv
    coreutils
    git
    nix
  ];
  text = ''
    test_root="$(mktemp -d -t nanopynix-tests.XXXXXX)"
    trap 'rm -rf "$test_root"' EXIT

    cp -R ${testSource} "$test_root/source"
    chmod -R u+w "$test_root/source"
    ln -s ${flakeCompatishSrc} "$test_root/flake-compatish"
    ln -s ${grpclibTransportsSrc} "$test_root/grpclab"

    export PYTHONNOUSERSITE=1
    export PYTHONPATH="$test_root/source/python/src:$test_root/source/pynix/src"
    cd "$test_root/source"
    python -m pytest -p no:cacheprovider tests "$@"
  '';
}
