{
  pkgs ? import <nixpkgs> { },
}:

pkgs.stdenvNoCC.mkDerivation {
  name = "ca-test";
  dontUnpack = true;
  __contentAddressed = true;
  outputHashAlgo = "sha256";
  outputHashMode = "recursive";
  buildPhase = ''
    echo "ca-content" > $out
  '';
  installPhase = "true";
}
