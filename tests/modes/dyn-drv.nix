{ system, ts }:

let
  mkDrv =
    { name, buildCommand }:
    derivation {
      inherit name system;
      builder = "/bin/sh";
      args = [
        "-c"
        buildCommand
      ];
      _timestamp = ts;
    };

  mkCADrv =
    {
      name,
      buildCommand,
      hashMode ? "recursive",
    }:
    derivation {
      inherit name system;
      builder = "/bin/sh";
      args = [
        "-c"
        buildCommand
      ];
      outputHashAlgo = "sha256";
      outputHashMode = hashMode;
      __contentAddressed = true;
      _timestamp = ts;
    };

  hello = mkDrv {
    name = "hello";
    buildCommand = "echo hello > $out";
  };

  producingDrv = mkCADrv {
    name = "hello.drv";
    hashMode = "text";
    buildCommand = ''
      while IFS= read -r line || [ -n "$line" ]; do
        printf '%s\n' "$line"
      done < "${builtins.unsafeDiscardOutputDependency hello.drvPath}" > $out
    '';
  };

  indirectHello = builtins.outputOf producingDrv.outPath "out";

  wrapper = mkDrv {
    name = "use-dynamic-drv";
    buildCommand = ''
      while IFS= read -r line || [ -n "$line" ]; do
        printf '%s\n' "$line"
      done < "${indirectHello}" > $out
    '';
  };

in
{
  inherit hello producingDrv wrapper;
}
