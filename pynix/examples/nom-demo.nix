# A derivation graph for trying `pynix build --nom`.
#
# Impure on purpose: `builtins.currentTime` changes every derivation on every
# run, so nothing is ever cached and the whole graph builds again.
# NOM_DEMO_FAIL=1 makes lib-ui fail in its checkPhase.
#
#   pynix build --nom --file pynix/examples/nom-demo.nix
#   NOM_DEMO_FAIL=1 pynix build --nom --file pynix/examples/nom-demo.nix
#
# The tools in `downloads` come from the binary cache, so they show as
# downloads the first time only.
{
  pkgs ? import <nixpkgs> { },
  seed ? toString builtins.currentTime,
  fail ? builtins.getEnv "NOM_DEMO_FAIL" == "1",
  downloads ? [
    pkgs.sl
    pkgs.figlet
    pkgs.cowsay
  ],
}:
let
  inherit (pkgs) lib;

  # One node: a build phase that takes `seconds`, and a check phase.
  node =
    name:
    {
      deps ? [ ],
      seconds,
      failing ? false,
      extraInputs ? [ ],
    }:
    pkgs.stdenvNoCC.mkDerivation {
      name = "nom-demo-${name}";
      inherit seed;
      dontUnpack = true;
      nativeBuildInputs = extraInputs;
      buildPhase = ''
        for i in $(seq ${toString seconds}); do
          echo "${name}: step $i of ${toString seconds}"
          sleep 1
        done
      '';
      doCheck = true;
      checkPhase = ''
        sleep 1
        ${lib.optionalString failing ''echo "${name}: the check fails on purpose"; exit 1''}
      '';
      installPhase = ''
        ${lib.concatMapStrings (d: "cat ${d} >> $out\n") deps}
        echo ${name} >> $out
      '';
    };

  codegen = node "codegen" { seconds = 4; };
  fetch-a = node "fetch-a" { seconds = 2; };
  fetch-b = node "fetch-b" { seconds = 3; };
  lib-core = node "lib-core" {
    deps = [
      fetch-a
      codegen
    ];
    seconds = 5;
  };
  lib-net = node "lib-net" {
    deps = [ fetch-b ];
    seconds = 3;
  };
  lib-ui = node "lib-ui" {
    deps = [ lib-core ];
    seconds = 4;
    failing = fail;
  };
  docs = node "docs" {
    deps = [ codegen ];
    seconds = 6;
  };
  app = node "app" {
    deps = [
      lib-core
      lib-net
      lib-ui
    ];
    seconds = 3;
    extraInputs = downloads;
  };
in
node "bundle" {
  deps = [
    app
    docs
  ];
  seconds = 2;
}
