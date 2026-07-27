{ lib }:
# Several names in pkgs.nixVersions are aliases for the exact same Nix
# derivation (e.g. `stable`/`latest`/`nix_2_34` commonly all resolve to the
# same build). Keep only one canonical name per distinct Nix derivation so
# downstream test packages, and CI, don't build/run the same Nix version
# twice under different names. Prefer explicit version-numbered names (e.g.
# `nix_2_34`) over generic rolling aliases (`stable`, `latest`), since the
# generic aliases point at different derivations over time.
versions:
let
  deprioritized = builtins.filter (name: builtins.hasAttr name versions) [
    "stable"
    "latest"
  ];
  orderedNames = lib.subtractLists deprioritized (builtins.attrNames versions) ++ deprioritized;
  pick =
    state: name:
    let
      drvPath = versions.${name}.nix.drvPath;
    in
    if builtins.elem drvPath state.seen then
      state
    else
      {
        seen = state.seen ++ [ drvPath ];
        names = state.names ++ [ name ];
      };
  picked = lib.foldl' pick {
    seen = [ ];
    names = [ ];
  } orderedNames;
in
lib.getAttrs picked.names versions
