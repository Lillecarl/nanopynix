# pynix-lsp: terranixEntry = import ../default.nix { }
#
# A real terranix author might wrap a module's definitions in an explicit
# `config = { ... };` (the NixOS-module convention -- see
# ../../module_system/config1.nix for the base ModuleSystemDialect version
# of this same config1/config2 pairing) instead of the flat/implicit style
# every other fixture in this directory uses. `TerranixDialect`'s roots come
# from `entry.attr("terranix").attr("config")` -- already fully merged by
# the module system, indifferent to which style the source used -- and
# `identifier_path_at`/`completion_target_at` only ever look at the
# innermost enclosing binding's own attrpath, never an outer `config` key.
# So `resource.random_id.id06` below should resolve identically whether or
# not it's wrapped in `config`. This fixture pins that down as a real
# regression test rather than something only checked by hand once.
#
# The marker comment below is for pynix/tests/test_lsp_scenarios.py (see
# pynix/tests/support/lsp_markers.py) -- not part of the terranix config itself.
{ ... }:
{
  config = {
    # LSLINE1
    resource.random_id.id06 = { };
  };
}
