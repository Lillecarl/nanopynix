"""Tests for nanopynix_flake (FlakeRef, parse_flake_ref, lock_flake, get_flake)."""

import nanopynix
import nanopynix_flake


class TestParseFlakeRef:
    def test_github_ref(self):
        ref = nanopynix.parse_flake_ref("github:NixOS/nixpkgs")
        assert isinstance(ref, nanopynix_flake.FlakeRef)
        s = str(ref)
        assert "github:NixOS/nixpkgs" in s or "github" in s.lower()

    def test_indirect_ref(self):
        ref = nanopynix.parse_flake_ref("nixpkgs")
        assert isinstance(ref, nanopynix_flake.FlakeRef)

    def test_repr(self):
        ref = nanopynix.parse_flake_ref("github:NixOS/nixpkgs")
        r = repr(ref)
        assert r.startswith("FlakeRef(")


class TestLockFlake:
    def test_lock_flake_nixpkgs(self, eval_state):
        ref = nanopynix.parse_flake_ref("github:NixOS/nixpkgs")
        locked = nanopynix.lock_flake(eval_state, ref)
        desc = locked.description()
        assert isinstance(desc, str)

    def test_lock_flake_inputs(self, eval_state):
        ref = nanopynix.parse_flake_ref("github:NixOS/nixpkgs")
        locked = nanopynix.lock_flake(eval_state, ref)
        inputs = locked.inputs()
        assert isinstance(inputs, dict)

    def test_lock_flake_repr(self, eval_state):
        ref = nanopynix.parse_flake_ref("github:NixOS/nixpkgs")
        locked = nanopynix.lock_flake(eval_state, ref)
        r = repr(locked)
        assert r.startswith("LockedFlake(")


class TestGetFlake:
    def test_get_flake(self, eval_state):
        ref = nanopynix.parse_flake_ref("github:NixOS/nixpkgs")
        resolved = nanopynix.get_flake(eval_state, ref)
        assert isinstance(resolved, nanopynix_flake.FlakeRef)
