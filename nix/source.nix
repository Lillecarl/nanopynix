/*
  The repository tree, filtered, for a derivation that needs the whole thing.

  The filter itself is `nix/clean-source.nix`, which also serves one project at
  a time. That file carries the measurements and the reason each entry is on
  the list. This one only says which tree to apply it to.

  Prefer a project source over this one. A derivation that reads the whole
  repository rebuilds when any file of any project changes, which is what issue
  #130 set out to stop.
*/
{ lib }:
import ./clean-source.nix { inherit lib; } {
  src = ../.;
  name = "nanopynix-source";
}
