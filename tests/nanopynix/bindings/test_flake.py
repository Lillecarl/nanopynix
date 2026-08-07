"""Tests for nanopynix_flake (FlakeRef, parse_flake_ref, lock_flake, get_flake, call_flake, eval_flake)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from nanopynix_bindings import errors as nanopynix_errors, flake as nanopynix_flake

import nanopynix
from tests.support.git import commit_files, init_flake_repo, init_repo

if TYPE_CHECKING:
    from pathlib import Path


# Locking a flake needs an evaluator, and this module builds one in the pytest
# process for every test -- through the `eval_state` fixture, and directly
# where a test needs its own fetch settings. See tests/support/nix_runtime.py.
pytestmark = pytest.mark.evaluator_in_process


def _init_git_flake(tmp_path: Path, outputs_body: str = "val = 1;") -> None:
    init_flake_repo(tmp_path, outputs_body)


def _dirty_the_flake(tmp_path: Path) -> None:
    """Make the working tree differ from the last commit.

    The dirtied file has to be a tracked one. An untracked file leaves the
    tree clean as far as the git fetcher is concerned.
    """
    flake_file = tmp_path / "flake.nix"
    flake_file.write_text(flake_file.read_text(encoding="utf-8") + "\n# dirtied\n", encoding="utf-8")


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

    def test_find_input_answers_none_for_a_flake_with_no_inputs(
        self,
        eval_state: nanopynix.EvalState,
        tmp_path: Path,
    ) -> None:
        """A name that no input carries must be nothing, and not an empty node.

        This replaces a test of ``locked.inputs()``, a flat map of the inputs
        the ``flake.nix`` *declared*. That map reported the original reference
        under a name that said locked, and it had nowhere to put a transitive
        node or a ``follows`` edge.
        """
        _init_git_flake(tmp_path)
        ref = nanopynix.parse_flake_ref(str(tmp_path))
        locked = nanopynix.lock_flake(eval_state, ref)
        assert locked.find_input(["nixpkgs"]) is None

    def test_find_input_answers_none_for_the_root(
        self,
        eval_state: nanopynix.EvalState,
        tmp_path: Path,
    ) -> None:
        """The empty path names the root, which is a Node and not a LockedNode.

        Nix's own caller casts with ``dynamic_pointer_cast`` for exactly this
        reason: the root carries no locked reference, so there is nothing to
        report about it.
        """
        _init_git_flake(tmp_path)
        ref = nanopynix.parse_flake_ref(str(tmp_path))
        locked = nanopynix.lock_flake(eval_state, ref)
        assert locked.find_input([]) is None

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
        self,
        eval_state: nanopynix.EvalState,
        tmp_path: Path,
    ) -> None:
        _init_git_flake(tmp_path)
        ref = nanopynix.parse_flake_ref(str(tmp_path))
        with pytest.raises(RuntimeError, match="unknown setting"):
            nanopynix.lock_flake(eval_state, ref, flake_settings={"not-a-real-setting": "1"})


class TestGitFetcherSettings:
    """Issue #34: a flake reference must own the fetch settings it points at.

    ``parse_flake_ref`` used to build the reference against a ``Settings`` on
    its own stack. Nix 2.31 keeps a pointer to that object inside the
    ``Input`` and its git fetcher reads through the pointer, so every lock of
    a ``git+file://`` reference read freed memory there. Nix 2.34 and 2.35
    pass ``state.fetchSettings`` to each fetcher call instead, which is why
    only 2.31 showed it.

    Every other test in this file uses a ``path:`` reference, which reaches no
    git fetcher at all. That is why nothing here caught it.
    """

    def test_a_clean_git_flake_locks(self, eval_state: nanopynix.EvalState, tmp_path: Path) -> None:
        """The plain case. On Nix 2.31 this raised ``RuntimeError: Invalid argument``.

        Nothing in Nix raised that message. It was ``pthread_mutex_lock``
        answering ``EINVAL`` for a mutex that had already been destroyed,
        inside the fetcher cache of the dead ``Settings``.
        """
        _init_git_flake(tmp_path, r'val = "clean-git-lock";')
        ref = nanopynix.parse_flake_ref(f"git+file://{tmp_path}")
        locked = nanopynix.lock_flake(eval_state, ref, write_lock_file=False)
        assert isinstance(locked.description(), str)

    def test_the_evaluator_refuses_a_dirty_tree_when_allow_dirty_is_off(
        self,
        store: Any,
        tmp_path: Path,
    ) -> None:
        """The evaluator's own fetch settings decide, and Nix authors the refusal."""
        _init_git_flake(tmp_path, r'val = "dirty-refused";')
        _dirty_the_flake(tmp_path)
        eval_state = nanopynix.EvalState(store, [], fetch_settings={"allow-dirty": "false"})
        ref = nanopynix.parse_flake_ref(f"git+file://{tmp_path}")
        with pytest.raises(nanopynix_errors.Error, match="dirty"):
            nanopynix.lock_flake(eval_state, ref, write_lock_file=False)

    def test_the_evaluator_accepts_a_dirty_tree_when_allow_dirty_is_on(
        self,
        store: Any,
        tmp_path: Path,
    ) -> None:
        """The other direction, so a setting that never arrives cannot pass both."""
        _init_git_flake(tmp_path, r'val = "dirty-accepted";')
        _dirty_the_flake(tmp_path)
        eval_state = nanopynix.EvalState(store, [], fetch_settings={"allow-dirty": "true"})
        ref = nanopynix.parse_flake_ref(f"git+file://{tmp_path}")
        locked = nanopynix.lock_flake(eval_state, ref, write_lock_file=False)
        assert isinstance(locked.description(), str)


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
        """,
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
