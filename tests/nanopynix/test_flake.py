"""Tests for nanopynix_flake (FlakeRef, parse_flake_ref, lock_flake, get_flake, call_flake, eval_flake)."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

import nanopynix
import nanopynix_flake


def _init_git_flake(tmp_path, outputs_body="val = 1;"):
    """Create a temp flake with a git repo so Nix can evaluate it."""
    (tmp_path / "flake.nix").write_text(f"""
    {{
        outputs = {{ ... }}: {{
            {outputs_body}
        }};
    }}
    """)
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "flake.nix"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)


class TestParseFlakeRef:
    def test_github_ref(self) -> None:
        ref = nanopynix.parse_flake_ref("github:NixOS/nixpkgs")  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        assert isinstance(ref, nanopynix_flake.FlakeRef)  # type: ignore[reportUnknownVariableType]  # ref type from nanobind extension
        s = str(ref)
        assert "github:NixOS/nixpkgs" in s or "github" in s.lower()

    def test_indirect_ref(self) -> None:
        ref = nanopynix.parse_flake_ref("nixpkgs")  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        assert isinstance(ref, nanopynix_flake.FlakeRef)  # type: ignore[reportUnknownVariableType]  # ref from nanobind

    def test_repr(self) -> None:
        ref = nanopynix.parse_flake_ref("github:NixOS/nixpkgs")  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        r = repr(ref)  # type: ignore[reportUnknownVariableType]  # ref type from nanobind
        assert r.startswith("FlakeRef(")


class TestLockFlake:
    def test_lock_flake_nixpkgs(self, eval_state: nanopynix.EvalState, tmp_path: Path) -> None:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        _init_git_flake(tmp_path)
        ref = nanopynix.parse_flake_ref(str(tmp_path))  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        locked = nanopynix.lock_flake(eval_state, ref)  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        desc = locked.description()  # type: ignore[reportUnknownMemberType]  # LockedFlake from nanobind
        assert isinstance(desc, str)

    def test_lock_flake_inputs(self, eval_state: nanopynix.EvalState, tmp_path: Path) -> None:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        _init_git_flake(tmp_path)
        ref = nanopynix.parse_flake_ref(str(tmp_path))  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension
        locked = nanopynix.lock_flake(eval_state, ref)  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension
        inputs = locked.inputs()  # type: ignore[reportUnknownMemberType]  # LockedFlake from nanobind
        assert isinstance(inputs, dict)

    def test_lock_flake_repr(self, eval_state: nanopynix.EvalState, tmp_path: Path) -> None:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        _init_git_flake(tmp_path)
        ref = nanopynix.parse_flake_ref(str(tmp_path))  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension
        locked = nanopynix.lock_flake(eval_state, ref)  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension
        r = repr(locked)
        assert r.startswith("LockedFlake(")


class TestGetFlake:
    def test_get_flake(self, eval_state: nanopynix.EvalState, tmp_path: Path) -> None:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        _init_git_flake(tmp_path)
        ref = nanopynix.parse_flake_ref(str(tmp_path))  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension
        resolved = nanopynix.get_flake(eval_state, ref)  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension
        assert isinstance(resolved, nanopynix_flake.FlakeRef)  # type: ignore[reportUnknownVariableType]  # resolved from nanobind


class TestCallFlake:
    def test_call_flake(self, eval_state: nanopynix.EvalState, tmp_path: Path) -> None:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        """call_flake evaluates a locked flake's outputs."""
        (tmp_path / "flake.nix").write_text("""
        {
            outputs = { ... }: {
                hello = "world";
                num = 42;
            };
        }
        """)
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "add", "flake.nix"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
        ref = nanopynix.parse_flake_ref(str(tmp_path))  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension
        locked = nanopynix.lock_flake(eval_state, ref, write_lock_file=False)  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension
        outputs = nanopynix_flake.call_flake(eval_state, locked)  # type: ignore[reportUnknownMemberType]  # nanopynix_flake nanobind extension
        outputs.force()  # type: ignore[reportUnknownMemberType]  # EvalState/NixValue from nanobind
        assert outputs.type_name() == "attrs"  # type: ignore[reportUnknownMemberType]  # NixValue from nanobind
        hello = outputs.attr_get("hello")  # type: ignore[reportUnknownMemberType]  # NixValue from nanobind
        assert hello.as_string() == "world"  # type: ignore[reportUnknownMemberType]  # NixValue from nanobind
        num = outputs.attr_get("num")  # type: ignore[reportUnknownMemberType]  # NixValue from nanobind
        assert num.as_int() == 42  # type: ignore[reportUnknownMemberType]  # NixValue from nanobind


class TestEvalFlake:
    def test_eval_flake(self, eval_state: nanopynix.EvalState, tmp_path: Path) -> None:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        """eval_flake locks and evaluates a flake in one step."""
        (tmp_path / "flake.nix").write_text("""
        {
            outputs = { ... }: {
                greeting = "hi";
                count = 7;
            };
        }
        """)
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "add", "flake.nix"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
        outputs = nanopynix_flake.eval_flake(eval_state, str(tmp_path), write_lock_file=False)  # type: ignore[reportUnknownMemberType]  # nanopynix_flake nanobind extension
        outputs.force()  # type: ignore[reportUnknownMemberType]  # NixValue from nanobind
        assert outputs.type_name() == "attrs"  # type: ignore[reportUnknownMemberType]  # NixValue from nanobind
        greeting = outputs.attr_get("greeting")  # type: ignore[reportUnknownMemberType]  # NixValue from nanobind
        assert greeting.as_string() == "hi"  # type: ignore[reportUnknownMemberType]  # NixValue from nanobind
        count = outputs.attr_get("count")  # type: ignore[reportUnknownMemberType]  # NixValue from nanobind
        assert count.as_int() == 7  # type: ignore[reportUnknownMemberType]  # NixValue from nanobind

    def test_eval_flake_writes_lock_file(self, eval_state: nanopynix.EvalState, tmp_path: Path) -> None:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        """eval_flake with write_lock_file=True creates flake.lock."""
        dep_dir = tmp_path / "dep"
        dep_dir.mkdir()
        _init_git_flake(dep_dir)

        (tmp_path / "flake.nix").write_text(
            """
        {
            inputs.dep.url = "DIR";
            outputs = { self, dep, ... }: {
                val = 1;
            };
        }
        """.replace("DIR", str(dep_dir))
        )
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "add", "flake.nix"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

        assert not (tmp_path / "flake.lock").exists()
        nanopynix_flake.eval_flake(eval_state, str(tmp_path), write_lock_file=True)  # type: ignore[reportUnknownMemberType]  # nanopynix_flake nanobind extension
        assert (tmp_path / "flake.lock").exists()

    def test_eval_flake_no_write_lock_file(self, eval_state: nanopynix.EvalState, tmp_path: Path) -> None:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        """eval_flake with write_lock_file=False does NOT create flake.lock."""
        _init_git_flake(tmp_path)
        assert not (tmp_path / "flake.lock").exists()
        nanopynix_flake.eval_flake(eval_state, str(tmp_path), write_lock_file=False)  # type: ignore[reportUnknownMemberType]  # nanopynix_flake nanobind extension
        assert not (tmp_path / "flake.lock").exists()


class TestWriteLockFile:
    def test_write_lock_file(self, eval_state: nanopynix.EvalState, tmp_path: Path) -> None:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        """lock_flake with write_lock_file=False, then write_lock_file() persists."""
        _init_git_flake(tmp_path)
        ref = nanopynix.parse_flake_ref(str(tmp_path))  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension
        locked = nanopynix.lock_flake(eval_state, ref, write_lock_file=False)  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension
        assert not (tmp_path / "flake.lock").exists()
        locked.write_lock_file()  # type: ignore[reportUnknownMemberType]  # LockedFlake from nanobind
        assert (tmp_path / "flake.lock").exists()
