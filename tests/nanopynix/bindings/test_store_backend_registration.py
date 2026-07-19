"""Tests for nanopynix_store.register_store_implementation."""

from __future__ import annotations

import pytest
from nanopynix_bindings import store as nanopynix_store

import nanopynix


class TestRegisterStore:
    def test_register_and_open(self):
        """Register a minimal store implementation and open it."""

        class MinimalStore:
            def is_valid_path_uncached(self, path_str: str) -> bool:
                return True

            def query_path_info(self, path_str: str) -> None:
                return None

            def query_path_from_hash_part(self, hash_part: str) -> None:
                return None

        class Factory:
            @staticmethod
            def open_store() -> object:
                return MinimalStore()

        nanopynix_store.register_store_implementation(
            "test-minimal-store",
            "A minimal Python store for testing",
            ["test-min"],
            Factory(),
        )

        store = nanopynix.open_store("test-min://example")
        assert isinstance(store, nanopynix_store.Store)
        sp = nanopynix_store.StorePath("00000000000000000000000000000000-bogus")
        assert store.is_valid_path(sp)

    def test_register_duplicate_name_raises(self):
        """Registering the same name twice should raise."""

        class Factory:
            @staticmethod
            def open_store() -> None:
                return None

        nanopynix_store.register_store_implementation(
            "test-dup-check",
            "First registration",
            ["test-dup"],
            Factory(),
        )

        with pytest.raises(RuntimeError, match="already registered"):
            nanopynix_store.register_store_implementation(
                "test-dup-check",
                "Second registration",
                ["test-dup2"],
                Factory(),
            )

    def test_multiple_schemes(self):
        """Store can be registered with multiple URI schemes."""

        class Factory:
            @staticmethod
            def open_store() -> object:
                class S:
                    def is_valid_path_uncached(self, p: str) -> bool:
                        return True

                    def query_path_info(self, p: str) -> None:
                        return None

                    def query_path_from_hash_part(self, h: str) -> None:
                        return None

                return S()

        nanopynix_store.register_store_implementation(
            "test-multi-scheme",
            "Store with multiple schemes",
            ["test-a", "test-b"],
            Factory(),
        )

        a = nanopynix.open_store("test-a://example")
        b = nanopynix.open_store("test-b://example")
        assert isinstance(a, nanopynix_store.Store)
        assert isinstance(b, nanopynix_store.Store)

    def test_is_valid_path_false(self):
        """Store returns False for unknown paths."""

        class Factory:
            def open_store(self) -> object:
                class S:
                    def is_valid_path_uncached(self, p: str) -> bool:
                        return False

                    def query_path_info(self, p: str) -> None:
                        return None

                    def query_path_from_hash_part(self, h: str) -> None:
                        return None

                return S()

        nanopynix_store.register_store_implementation(
            "test-valid-false",
            "Store where nothing is valid",
            ["test-noval"],
            Factory(),
        )

        store = nanopynix.open_store("test-noval://example")
        sp = nanopynix_store.StorePath("00000000000000000000000000000000-any")
        assert not store.is_valid_path(sp)

    def test_is_valid_path_from_python_store(self):
        """Python store's is_valid_path_uncached controls validity."""

        class Factory:
            def open_store(self) -> object:
                class S:
                    def __init__(self) -> None:
                        self.valid: set[str] = {
                            "00000000000000000000000000000001-yes",
                        }

                    def is_valid_path_uncached(self, p: str) -> bool:
                        return p in self.valid

                    def query_path_info(self, p: str) -> None:
                        return None

                    def query_path_from_hash_part(self, h: str) -> None:
                        return None

                return S()

        nanopynix_store.register_store_implementation(
            "test-valid-control",
            "Store that validates specific paths",
            ["test-val"],
            Factory(),
        )

        store = nanopynix.open_store("test-val://example")
        yes = nanopynix_store.StorePath("00000000000000000000000000000001-yes")
        no = nanopynix_store.StorePath("00000000000000000000000000000000-no")
        assert store.is_valid_path(yes)
        assert not store.is_valid_path(no)
