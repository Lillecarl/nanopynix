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

  # ── Deep chain: 5 layers of nested outputOf ──────────────────
  #
  # builtins.outputOf wraps its first argument in a
  # SingleDerivedPath::Built, creating a nested struct that can be
  # chained arbitrarily deep.  At instantiation time this produces
  # a DownstreamPlaceholder string embedded in the build env.
  #
  # The outermost wrapper's .drv gets a dynamic_input_drvs entry
  # with 5 levels of childMap nesting:
  #   producer!out!out!out!out!out = target!out

  target = mkDrv {
    name = "target";
    buildCommand = ''printf '%s' deep-target > $out'';
  };

  producer = mkCADrv {
    name = "target.drv";
    hashMode = "text";
    buildCommand = ''
      while IFS= read -r line || [ -n "$line" ]; do
        printf '%s\n' "$line"
      done < "${builtins.unsafeDiscardOutputDependency target.drvPath}" > $out
    '';
  };

  # Chain 5 levels deep.  Each outputOf wraps the previous in
  # another SingleDerivedPath::Built{drvPath=prev, output="out"}.
  fiveDeep = builtins.outputOf
    (builtins.outputOf
      (builtins.outputOf
        (builtins.outputOf
          (builtins.outputOf
            producer.outPath
            "out")
          "out")
        "out")
      "out")
    "out";

  deepWrapper = mkDrv {
    name = "deep-5";
    buildCommand = ''
      while IFS= read -r line || [ -n "$line" ]; do
        printf '%s\n' "$line"
      done < "${fiveDeep}" > $out
    '';
  };

in
{
  inherit hello producingDrv wrapper target producer deepWrapper;
}
