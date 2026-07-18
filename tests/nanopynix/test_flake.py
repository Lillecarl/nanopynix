"""Tests for nanopynix_flake (FlakeRef, parse_flake_ref, lock_flake, get_flake, call_flake, eval_flake)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from _git import commit_files, init_flake_repo, init_repo

import nanopynix
import nanopynix_flake

if TYPE_CHECKING:
    from pathlib import Path


def _init_git_flake(tmp_path: Path, outputs_body: str = "val = 1;") -> None:
    init_flake_repo(tmp_path, outputs_body)


class TestParseFlakeRef:
    def test_github_ref(self) -> None:
        ref = nanopynix.parse_flake_ref("github:NixOS/nixpkgs")
        assert isinstance(ref, nanopynix_flake.FlakeRef)
        s = str(ref)
        assert "github:NixOS/nixpkgs" in s or "github" in s.lower()

    def test_indirect_ref(self) -> None:
        ref = nanopynix.parse_flake_ref("nixpkgs")
        assert isinstance(ref, nanopynix_flake.FlakeRef)

    def test_repr(self) -> None:
        ref = nanopynix.parse_flake_ref("github:NixOS/nixpkgs")
        r = repr(ref)
        assert r.startswith("FlakeRef(")


class TestLockFlake:
    def test_lock_flake_nixpkgs(self, eval_state: nanopynix.EvalState, tmp_path: Path) -> None:
        _init_git_flake(tmp_path)
        ref = nanopynix.parse_flake_ref(str(tmp_path))
        locked = nanopynix.lock_flake(eval_state, ref)
        desc = locked.description()
        assert isinstance(desc, str)

    def test_lock_flake_inputs(self, eval_state: nanopynix.EvalState, tmp_path: Path) -> None:
        _init_git_flake(tmp_path)
        ref = nanopynix.parse_flake_ref(str(tmp_path))
        locked = nanopynix.lock_flake(eval_state, ref)
        inputs = locked.inputs()
        assert isinstance(inputs, dict)

    def test_lock_flake_repr(self, eval_state: nanopynix.EvalState, tmp_path: Path) -> None:
        _init_git_flake(tmp_path)
        ref = nanopynix.parse_flake_ref(str(tmp_path))
        locked = nanopynix.lock_flake(eval_state, ref)
        r = repr(locked)
        assert r.startswith("LockedFlake(")

    def test_lock_flake_accepts_flake_settings(self, eval_state: nanopynix.EvalState, tmp_path: Path) -> None:
        """flake_settings reaches Nix's flake::Settings via Config::set, not silently dropped."""
        _init_git_flake(tmp_path)
        ref = nanopynix.parse_flake_ref(str(tmp_path))
        locked = nanopynix.lock_flake(eval_state, ref, flake_settings={"use-registries": "false"})
        assert isinstance(locked.description(), str)

    def test_lock_flake_rejects_unknown_flake_setting(
        self, eval_state: nanopynix.EvalState, tmp_path: Path
    ) -> None:
        _init_git_flake(tmp_path)
        ref = nanopynix.parse_flake_ref(str(tmp_path))
        with pytest.raises(RuntimeError, match="unknown setting"):
            nanopynix.lock_flake(eval_state, ref, flake_settings={"not-a-real-setting": "1"})


class TestGetFlake:
    def test_get_flake(self, eval_state: nanopynix.EvalState, tmp_path: Path) -> None:
        _init_git_flake(tmp_path)
        ref = nanopynix.parse_flake_ref(str(tmp_path))
        resolved = nanopynix.get_flake(eval_state, ref)
        assert isinstance(resolved, nanopynix_flake.FlakeRef)


class TestCallFlake:
    def test_call_flake(self, eval_state: nanopynix.EvalState, tmp_path: Path) -> None:
        """call_flake evaluates a locked flake's outputs."""
        init_flake_repo(tmp_path, r'hello = "world"; num = 42;')
        ref = nanopynix.parse_flake_ref(str(tmp_path))
        locked = nanopynix.lock_flake(eval_state, ref, write_lock_file=False)
        outputs = nanopynix_flake.call_flake(eval_state, locked)
        outputs.force()
        assert outputs.type_name() == "attrs"
        hello = outputs.attr_get("hello")
        assert hello.as_string() == "world"
        num = outputs.attr_get("num")
        assert num.as_int() == 42


class TestEvalFlake:
    def test_eval_flake(self, eval_state: nanopynix.EvalState, tmp_path: Path) -> None:
        """eval_flake locks and evaluates a flake in one step."""
        init_flake_repo(tmp_path, r'greeting = "hi"; count = 7;')
        outputs = nanopynix_flake.eval_flake(eval_state, str(tmp_path), write_lock_file=False)
        outputs.force()
        assert outputs.type_name() == "attrs"
        greeting = outputs.attr_get("greeting")
        assert greeting.as_string() == "hi"
        count = outputs.attr_get("count")
        assert count.as_int() == 7

    def test_eval_flake_writes_lock_file(self, eval_state: nanopynix.EvalState, tmp_path: Path) -> None:
        """eval_flake with write_lock_file=True creates flake.lock."""
        dep_dir = tmp_path / "dep"
        dep_dir.mkdir()
        init_flake_repo(dep_dir)

        (tmp_path / "flake.nix").write_text(
            f"""
        {{
            inputs.dep.url = "{dep_dir}";
            outputs = {{ self, dep, ... }}: {{
                val = 1;
            }};
        }}
        """
        )
        repo = init_repo(tmp_path)
        commit_files(repo, tmp_path / "flake.nix")

        assert not (tmp_path / "flake.lock").exists()
        nanopynix_flake.eval_flake(eval_state, str(tmp_path), write_lock_file=True)
        assert (tmp_path / "flake.lock").exists()

    def test_eval_flake_no_write_lock_file(self, eval_state: nanopynix.EvalState, tmp_path: Path) -> None:
        """eval_flake with write_lock_file=False does NOT create flake.lock."""
        _init_git_flake(tmp_path)
        assert not (tmp_path / "flake.lock").exists()
        nanopynix_flake.eval_flake(eval_state, str(tmp_path), write_lock_file=False)
        assert not (tmp_path / "flake.lock").exists()


class TestWriteLockFile:
    def test_write_lock_file(self, eval_state: nanopynix.EvalState, tmp_path: Path) -> None:
        """lock_flake with write_lock_file=False, then write_lock_file() persists."""
        _init_git_flake(tmp_path)
        ref = nanopynix.parse_flake_ref(str(tmp_path))
        locked = nanopynix.lock_flake(eval_state, ref, write_lock_file=False)
        assert not (tmp_path / "flake.lock").exists()
        locked.write_lock_file()
        assert (tmp_path / "flake.lock").exists()
