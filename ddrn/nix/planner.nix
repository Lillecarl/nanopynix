# `mkPlanner` builds a derivation whose output is another derivation.
#
# The output is text-hashed, which is what makes Nix read it back as a
# derivation rather than as a file. `builtins.outputOf` then names the output
# of the derivation that the planner wrote.
#
# The planner needs `experimental-features = ca-derivations
# dynamic-derivations`.
{
  lib,
  python3,
  runCommand,
}:

let
  # The menu entry of one candidate.
  #
  # `unsafeDiscardOutputDependency` keeps the `.drv` file as an input source of
  # the planner while the *output* of that derivation stays unbuilt. Nix writes
  # every `.drv` file at instantiation, so this costs no build. Without it, the
  # planner would depend on the output, and Nix would build every candidate
  # before the planner could reject any of them.
  #
  # `unsafeDiscardStringContext` does the same for the output path. The planner
  # learns where the output will land, and gains no dependency on it.
  candidate =
    {
      drv,
      name ? drv.name,
      outputs ? drv.outputs or [ "out" ],
      meta ? { },
    }:
    {
      inherit name meta;
      drv = builtins.unsafeDiscardOutputDependency drv.drvPath;
      outputs = lib.listToAttrs (
        map (output: lib.nameValuePair output (builtins.unsafeDiscardStringContext drv.${output}.outPath)) outputs
      );
    };

in
{
  inherit candidate;

  # The library that a planner imports, as a store path.
  #
  # A planner cannot install a package, so `ddrn` reaches it as a plain
  # directory on `PYTHONPATH`.
  ddrnPath = ../src;

  /**
    Build a derivation whose output is a derivation.

    - `name`: the name of the derivation that the planner *emits*. The planner
      itself is named `${name}.drv`, because its output is that file.
    - `plan`: the planner script, a path to a Python file.
    - `candidates`: the menu, a list of `{ drv, name ? , outputs ? , meta ? }`.
      Nix instantiates each one and builds none of them.
    - `tools`: an attribute set of store paths that the *emitted* derivation
      may name. Each one becomes an input of the planner, so it is built
      before the planner runs and is valid when the emitted derivation runs.
    - `env`: extra environment for the planner itself.
    - `python`: the interpreter that runs the planner.
    - `pythonPath`: extra directories for the `PYTHONPATH` of the planner.
      This is where a library that the *plan* needs goes, such as `packaging`.
      Nothing on it reaches the derivation that the planner emits.

    Give a library to `pythonPath`, and not to `python` through
    `withPackages`. Nix runs a builder with the *basename* of the builder as
    `argv[0]`, and CPython finds `sys.prefix` from `argv[0]`. A `withPackages`
    wrapper therefore falls back to the prefix of the interpreter it wraps,
    and the packages of the environment are invisible. The failure is a plain
    `ModuleNotFoundError` inside the build, which says nothing about the
    cause.

    Returns an attribute set with `planner` (the derivation that emits) and
    `outPath` (the output of the derivation that the planner emitted, through
    `builtins.outputOf`).
  */
  mkPlanner =
    {
      name,
      plan,
      candidates ? [ ],
      tools ? { },
      env ? { },
      python ? python3,
      system ? python3.stdenv.hostPlatform.system,
      pythonPath ? [ ],
    }:
    let
      ddrnLib = ../src;

      planner = derivation (
        env
        // {
          inherit system;
          name = "${name}.drv";
          builder = "${python}/bin/python3";
          args = [ plan ];

          PYTHONPATH = lib.concatStringsSep ":" ([ "${ddrnLib}" ] ++ pythonPath);
          # A planner writes exactly one file. A `.pyc` beside the source would
          # be written into the source path, which is read-only anyway, so this
          # only removes a warning.
          PYTHONDONTWRITEBYTECODE = "1";

          DDRN_SYSTEM = system;
          DDRN_STORE_DIR = builtins.storeDir;
          DDRN_MENU = builtins.toJSON (map candidate candidates);
          DDRN_TOOLS = lib.concatStringsSep " " (
            lib.mapAttrsToList (toolName: path: "${toolName}=${path}") tools
          );

          # The three attributes that make the output a derivation rather than
          # a file. `text` ingestion is the only mode that `dynamic-derivations`
          # accepts for this.
          __contentAddressed = true;
          outputHashMode = "text";
          outputHashAlgo = "sha256";
        }
      );
    in
    {
      inherit planner;

      # The output of the derivation that the planner emitted.
      outPath = builtins.outputOf planner.outPath "out";

      # A realisable derivation wrapping that output, for `nix build`. Building
      # `outPath` directly needs the `^out^out` installable syntax, which not
      # every entry point accepts yet.
      result = runCommand "${name}-result" { } ''
        cp -r ${builtins.outputOf planner.outPath "out"} $out
      '';
    };
}
