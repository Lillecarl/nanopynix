{
  system ? builtins.currentSystem,
}:
let
  mkCADrv =
    {
      name,
      buildCommand,
      outputs ? [ "out" ],
      hashAlgo ? "sha256",
      hashMode ? "recursive",
    }:
    derivation {
      inherit name system outputs;
      builder = "/bin/sh";
      args = [
        "-c"
        buildCommand
      ];
      outputHashAlgo = hashAlgo;
      outputHashMode = hashMode;
      __contentAddressed = true;
    };

  mkDrv =
    { name, buildCommand }:
    derivation {
      inherit name system;
      builder = "/bin/sh";
      args = [
        "-c"
        buildCommand
      ];
    };

  ca_simple = mkCADrv {
    name = "ca-simple";
    buildCommand = "echo ca-content > $out";
  };

  ca_multi_output = mkCADrv {
    name = "ca-multi";
    buildCommand = ''
      mkdir -p $out $dev
      echo out-content > $out
      echo dev-content > $dev
    '';
    outputs = [
      "out"
      "dev"
    ];
  };

  ca_depends_on_ca = mkCADrv {
    name = "ca-depends-on-ca";
    buildCommand = "echo dep-on-${ca_simple} > $out";
  };

  non_ca_depends_on_ca = mkDrv {
    name = "non-ca-depends-on-ca";
    buildCommand = "echo dep-on-${ca_simple} > $out";
  };
in
{
  inherit
    ca_simple
    ca_multi_output
    ca_depends_on_ca
    non_ca_depends_on_ca
    ;
}
