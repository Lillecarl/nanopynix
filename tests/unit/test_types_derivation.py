"""Unit tests for pynixd.types.derivation — OutputKind, DerivationOutput, BasicDerivation.

Tests the classification logic and property accessors for derivation types.
All tests are pure — no I/O, no wire protocol.
"""

from __future__ import annotations

import pytest

from pynixd.store_path import StorePath
from pynixd.types.derivation import BasicDerivation, DerivationOutput, OutputKind


class TestOutputKind:
    def test_input_addressed(self):
        out = DerivationOutput(path="/nix/store/abc-foo", method="", hash_digest="")
        assert out.kind == OutputKind.INPUT_ADDRESSED

    def test_ca_fixed(self):
        out = DerivationOutput(
            path="/nix/store/abc-foo",
            method="sha256",
            hash_digest="abc123",
        )
        assert out.kind == OutputKind.CA_FIXED

    def test_ca_floating(self):
        out = DerivationOutput(path="", method="sha256", hash_digest="")
        assert out.kind == OutputKind.CA_FLOATING

    def test_deferred(self):
        out = DerivationOutput(path="", method="", hash_digest="")
        assert out.kind == OutputKind.DEFERRED

    def test_impure(self):
        out = DerivationOutput(path="", method="sha256", hash_digest="impure")
        assert out.kind == OutputKind.IMPURE


class TestDerivationOutputProperties:
    def test_is_ca_fixed(self):
        out = DerivationOutput(path="/nix/store/abc", method="sha256", hash_digest="xyz")
        assert out.is_ca
        assert out.is_fixed_ca
        assert not out.is_floating_ca
        assert not out.is_deferred
        assert not out.is_impure

    def test_is_ca_floating(self):
        out = DerivationOutput(path="", method="sha256", hash_digest="")
        assert out.is_ca
        assert not out.is_fixed_ca
        assert out.is_floating_ca
        assert not out.is_deferred
        assert not out.is_impure

    def test_is_deferred(self):
        out = DerivationOutput(path="", method="", hash_digest="")
        assert not out.is_ca
        assert not out.is_fixed_ca
        assert not out.is_floating_ca
        assert out.is_deferred
        assert not out.is_impure

    def test_is_impure(self):
        out = DerivationOutput(path="", method="sha256", hash_digest="impure")
        assert out.is_ca
        assert not out.is_fixed_ca
        assert not out.is_floating_ca
        assert not out.is_deferred
        assert out.is_impure

    def test_is_text_hashed(self):
        out = DerivationOutput(path="/nix/store/abc", method="text:sha256", hash_digest="xyz")
        assert out.is_text_hashed

    def test_is_not_text_hashed(self):
        out = DerivationOutput(path="/nix/store/abc", method="sha256", hash_digest="xyz")
        assert not out.is_text_hashed

    def test_is_dynamic_output(self):
        out = DerivationOutput(path="", method="text:sha256", hash_digest="")
        assert out.is_dynamic_output

    def test_is_not_dynamic_output(self):
        out = DerivationOutput(path="", method="sha256", hash_digest="")
        assert not out.is_dynamic_output


class TestBasicDerivation:
    def test_requires_nix_false(self):
        """A simple input-addressed derivation should support Lix."""
        drv = BasicDerivation(
            outputs={"out": DerivationOutput(path="/nix/store/abc-foo")},
            platform="x86_64-linux",
            builder="/bin/sh",
            args=["-c", "true"],
        )
        assert drv.supports_lix()
        assert not drv.requires_nix

    def test_requires_nix_true_ca(self):
        """A CA derivation should require Nix."""
        drv = BasicDerivation(
            outputs={"out": DerivationOutput(path="", method="sha256", hash_digest="")},
        )
        assert not drv.supports_lix()
        assert drv.requires_nix

    def test_requires_nix_true_deferred(self):
        """A deferred derivation should require Nix."""
        drv = BasicDerivation(
            outputs={"out": DerivationOutput(path="", method="", hash_digest="")},
        )
        assert not drv.supports_lix()
        assert drv.requires_nix

    def test_dynamic_requires_nix(self):
        """Dynamic derivations always require Nix, regardless of output types."""
        drv = BasicDerivation(
            outputs={"out": DerivationOutput(path="/nix/store/abc-foo")},
            is_dynamic=True,
        )
        assert not drv.supports_lix()
        assert drv.requires_nix

    def test_build_local_true(self):
        drv = BasicDerivation(env={"preferLocalBuild": "1"})
        assert drv.build_local

    def test_build_local_pynixd_fast(self):
        drv = BasicDerivation(env={"pynixd_fast": "1"})
        assert drv.build_local

    def test_build_local_false(self):
        drv = BasicDerivation(env={})
        assert not drv.build_local

    def test_required_system_features_empty(self):
        drv = BasicDerivation(env={})
        assert drv.required_system_features == set()

    def test_required_system_features_parsed(self):
        drv = BasicDerivation(env={"requiredSystemFeatures": "kvm big-parallel"})
        assert drv.required_system_features == {"kvm", "big-parallel"}

    def test_output_paths(self):
        drv = BasicDerivation(
            outputs={
                "out": DerivationOutput(path="/nix/store/abc-foo"),
                "lib": DerivationOutput(path="/nix/store/abc-lib"),
            },
        )
        paths = drv.output_paths()
        assert paths == {
            "out": StorePath("/nix/store/abc-foo"),
            "lib": StorePath("/nix/store/abc-lib"),
        }

    def test_serialize_for_stats(self):
        drv = BasicDerivation(
            outputs={"out": DerivationOutput(path="/nix/store/abc-foo")},
            platform="x86_64-linux",
            builder="/bin/sh",
            args=["-c", "echo hi"],
            env={"foo": "bar", "PATH": "/bin", "NIX_STUFF": "x"},
        )
        result = drv.serialize_for_stats()
        assert "B:/bin/sh" in result
        assert "A:-c echo hi" in result
        assert "foo" in result
        # NIX_* and common noisy keys should be excluded
        assert "NIX_STUFF" not in result

    def test_has_dynamic_outputs_true(self):
        drv = BasicDerivation(
            outputs={"out": DerivationOutput(path="", method="text:sha256", hash_digest="")},
        )
        assert drv.has_dynamic_outputs

    def test_has_dynamic_outputs_false(self):
        drv = BasicDerivation(
            outputs={"out": DerivationOutput(path="/nix/store/abc-foo")},
        )
        assert not drv.has_dynamic_outputs

    def test_has_ca_floating_true(self):
        drv = BasicDerivation(
            outputs={"out": DerivationOutput(path="", method="sha256", hash_digest="")},
        )
        assert drv.has_ca_floating

    def test_has_ca_floating_false_for_text(self):
        drv = BasicDerivation(
            outputs={"out": DerivationOutput(path="", method="text:sha256", hash_digest="")},
        )
        # text:sha256 with empty hash is a dynamic output, not CA floating
        assert not drv.has_ca_floating

    def test_has_deferred_true(self):
        drv = BasicDerivation(
            outputs={"out": DerivationOutput(path="", method="", hash_digest="")},
        )
        assert drv.has_deferred

    def test_has_impure_true(self):
        drv = BasicDerivation(
            outputs={"out": DerivationOutput(path="", method="sha256", hash_digest="impure")},
        )
        assert drv.has_impure

    def test_has_text_hashed_true(self):
        drv = BasicDerivation(
            outputs={
                "out": DerivationOutput(path="/nix/store/abc", method="text:sha256", hash_digest="xyz"),
            },
        )
        assert drv.has_text_hashed

    def test_effective_required_features(self):
        from pynixd.system_features import PYNIXD_HANDLED_FEATURES

        drv = BasicDerivation(env={"requiredSystemFeatures": "kvm ca-derivations"})
        effective = drv.effective_required_features
        assert "ca-derivations" in effective  # no longer stripped (Lix can't serve CA)
        assert effective == {"kvm", "ca-derivations"}


class TestExhaustiveOutputKind:
    """Test all 5 branches of DerivationOutput.kind exhaustively."""

    @pytest.mark.parametrize(
        "name,out,expected",
        [
            ("INPUT_ADDRESSED", DerivationOutput(path="/p", method="", hash_digest=""), OutputKind.INPUT_ADDRESSED),
            ("CA_FIXED", DerivationOutput(path="/p", method="sha256", hash_digest="h"), OutputKind.CA_FIXED),
            ("CA_FLOATING", DerivationOutput(path="", method="sha256", hash_digest=""), OutputKind.CA_FLOATING),
            ("DEFERRED", DerivationOutput(path="", method="", hash_digest=""), OutputKind.DEFERRED),
            ("IMPURE", DerivationOutput(path="", method="sha256", hash_digest="impure"), OutputKind.IMPURE),
        ],
    )
    def test_kind(self, name, out, expected):
        assert out.kind == expected, f"{name} failed"
