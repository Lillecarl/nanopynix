# Laziness: Nix instantiates every candidate and builds only the ones that the
# planner names.
#
# A candidate that the planner rejects never runs its builder, so its output
# never enters the store. `select-check.sh` proves that.
{
  stdenv,
  bash,
  coreutils,
  ddrn,
}:

let
  # Stand-ins for the artefacts of a lock file. Nothing here is special: each
  # one is an ordinary derivation, and the point is that Nix does not build it.
  artefact =
    { name, tag }:
    derivation {
      inherit name;
      system = stdenv.hostPlatform.system;
      builder = "${bash}/bin/bash";
      args = [
        "-c"
        ''printf '%s built for %s\n' "$name" "$TAG" > "$out"''
      ];
      TAG = tag;
    };

  artefacts = [
    {
      name = "wheel-linux";
      tag = "manylinux";
    }
    {
      name = "wheel-macos";
      tag = "macosx";
    }
    {
      name = "wheel-windows";
      tag = "win_amd64";
    }
    {
      name = "wheel-any";
      tag = "py3-none-any";
    }
  ];
in
ddrn.mkPlanner {
  name = "selected-wheels";
  plan = ./plan.py;
  tools = { inherit bash coreutils; };
  # `WANTED_TAGS` stands in for the compatibility tags of the host. A real
  # planner computes this with `packaging.tags`, which is the whole point:
  # that computation has no reasonable expression in the Nix language.
  env.WANTED_TAGS = "manylinux py3-none-any";
  candidates = map (a: {
    drv = artefact a;
    inherit (a) name;
    meta = { inherit (a) tag; };
  }) artefacts;
}
