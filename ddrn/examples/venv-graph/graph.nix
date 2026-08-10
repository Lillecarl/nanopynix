# The graph, as a function of the plan that `plan.py` decided.
#
# **`plan.py` decides what the graph is, and this file says how a node is
# built.** The split is the whole argument of `ddrn`: every packaging decision
# needs `packaging`, which is Python, and every build step needs a derivation,
# which is Nix. Neither half reimplements the other.
#
# The evaluator that reads this file runs **inside** the sandbox of the
# planner. It writes each derivation below through the restricted daemon
# socket, and the planner then submits the root.
{
  system,
  pythonVersion,
  # The store path of each tool. Each one is an input of the planner, so
  # `builtins.storePath` may name it.
  tools,
  # The store path of each build script, on the same terms.
  scripts,
  # One entry for each node: `{ name, kind, artifact, backend, backendPath }`.
  nodes,
  # The name of the node that holds `pypa/installer`.
  installer,
  # `{ name, members, installed }`. A member is the name of a node.
  venv,
}:

let
  # **`builtins.storePath` is the primop that the released allowlist refuses.**
  # It reaches `EnsurePath`, and the restricted builder answers by asserting
  # that the path is an input of this build. Without the change of this lab,
  # evaluation stops here.
  storePaths = builtins.mapAttrs (_: builtins.storePath);

  tool = storePaths tools;
  script = storePaths scripts;

  # **This is what makes a pre-instantiated fetch a real dependency.**
  # `default.nix` hands the planner the store path of each `fetchurl`
  # derivation and the store path of its output, with the dependency between
  # them discarded, so that Nix instantiates every artefact of the lock file
  # and downloads none of them. `builtins.appendContext` puts the dependency
  # back for the artefacts that the planner chose, and it reaches `EnsurePath`
  # too.
  fetched =
    artifact:
    builtins.appendContext artifact.out {
      ${artifact.drv} = {
        outputs = [ "out" ];
      };
    };

  # Every node floats. A floating output has no path until the build gives it
  # one, so a node that reads another node interpolates a placeholder, and
  # neither the planner nor this file does any hash arithmetic.
  floating = {
    __contentAddressed = true;
    outputHashMode = "recursive";
    outputHashAlgo = "sha256";
  };

  site = "lib/python${pythonVersion}/site-packages";

  mk =
    args:
    derivation (
      {
        inherit system site;
        builder = "${tool.bash}/bin/bash";
        PATH = "${tool.coreutils}/bin:${tool.unzip}/bin:${tool.gnutar}/bin:${tool.gzip}/bin:${tool.python}/bin";
      }
      // args
      // floating
    );

  # The `site-packages` of a node that this graph builds. A node output floats,
  # so this is a downstream placeholder until the node is built.
  sitePath = name: "${built.${name}}/${site}";

  # **The installer is a node, so every install waits for it.** `pypa/installer`
  # is what writes `RECORD`, the `.dist-info` and each console script, and the
  # planner resolved it from the lock file like every other artefact.
  installerPath = sitePath installer;

  build =
    node:
    if node.kind == "unpack" then
      # The bootstrap. Nothing can install the installer, so its node unzips
      # the wheel and puts it on a path.
      mk {
        name = node.name;
        args = [
          "-c"
          ". ${script.unpackWheel}"
        ];
        wheel = fetched node.artifact;
      }
    else if node.kind == "wheel" then
      mk {
        name = node.name;
        args = [
          "-c"
          ". ${script.installWheel}"
        ];
        wheel = fetched node.artifact;
        wheelName = node.artifact.filename;
        inherit installerPath;
      }
    else if node.kind == "sdist" then
      mk {
        name = node.name;
        args = [
          "-c"
          ". ${script.buildSdist}"
        ];
        sdist = fetched node.artifact;
        inherit (node) backend;
        inherit installerPath;
        # The backend comes from the nodes that this graph already builds, and
        # from nowhere else. `built.${name}` is a derivation of this same
        # evaluation, so the dependency is real and the order is right.
        backendPath = builtins.concatStringsSep ":" (map sitePath node.backendPath);
      }
    else
      throw "unknown node kind '${node.kind}'";

  built = builtins.listToAttrs (
    map (node: {
      inherit (node) name;
      value = build node;
    }) nodes
  );
in
# The root node makes a real virtual environment, with `venv.EnvBuilder`, and
# merges the members into it. `make-venv.py` runs under the interpreter that
# the environment is for, which is what `EnvBuilder` takes as its base.
mk {
  inherit (venv) name;
  args = [
    "-c"
    ''exec "${tool.python}/bin/python3" "${script.makeVenv}"''
  ];
  members = builtins.concatStringsSep " " (map (name: "${built.${name}}") venv.members);
  installed = builtins.concatStringsSep "\n" venv.installed;
}
