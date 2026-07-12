"""Tests for nanopynix_util (settings, init, experimental features)."""

import nanopynix


class TestInitLibstore:
    def test_init_without_config(self):
        """init_libstore should work with load_config=False."""
        # Already called in conftest session fixture; call again to confirm
        nanopynix.init_libstore(load_config=False)

    def test_init_with_config(self):
        """init_libstore should work with load_config=True."""
        nanopynix.init_libstore(load_config=True)


class TestSettings:
    def test_get_setting_keep_going(self):
        nanopynix.set_setting("keep-going", "false")
        assert nanopynix.get_setting("keep-going") == "false"

    def test_get_setting_nonexistent(self):
        assert nanopynix.get_setting("this-setting-does-not-exist-xyz") is None

    def test_set_and_get_max_jobs(self):
        nanopynix.set_setting("max-jobs", "4")
        assert nanopynix.get_setting("max-jobs") == "4"

    def test_set_setting_unknown_raises(self):
        import pytest

        with pytest.raises(RuntimeError, match="unknown setting"):
            nanopynix.set_setting("not-a-real-setting-xyz", "value")

    def test_list_settings_includes_known(self):
        settings = nanopynix.list_settings()
        assert isinstance(settings, dict)
        # keep-going should appear after we set it above
        assert "keep-going" in settings


class TestExperimentalFeatures:
    def test_enable_flakes(self):
        nanopynix.enable_experimental_feature("flakes")
        # Should not raise

    def test_enable_unknown_raises(self):
        import pytest

        with pytest.raises(RuntimeError, match="unknown experimental feature"):
            nanopynix.enable_experimental_feature("not-a-real-feature-xyz")

    def test_enable_nix_command(self):
        nanopynix.enable_experimental_feature("nix-command")
