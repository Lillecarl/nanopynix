# pynix-lsp: moduleEntry = import ./default.nix { extraModules = [ ./last_good_tree.nix ]; }
#
# Regression fixture for the last-good-tree fallback (see
# pynix/tests/test_lsp.py's test_hover_falls_back_to_the_last_error_free_parse):
# the "enable" line below gets swapped for an incomplete `formals` list
# in-memory, which corrupts the `port` binding's own parse tree shape
# without changing the file's line count -- so `port`'s cursor position is
# identical between the clean and corrupted snapshots.
{ config, pkgs, lib, ... }:
{
  config = {
    services.example-daemon.enable = true;
    services.example-daemon.port = 9090;
  };
}
