"""Tests for nanopynix_main (init_nix, init_plugins)."""

import nanopynix


class TestInitNix:
    def test_init_nix_without_config(self):
        """init_nix should work without loading nix.conf."""
        nanopynix.init_nix(load_config=False)

    def test_init_nix_with_config(self):
        """init_nix should work with loading nix.conf."""
        nanopynix.init_nix(load_config=True)


class TestInitPlugins:
    def test_init_plugins(self):
        """init_plugins should not raise."""
        nanopynix.init_plugins()
