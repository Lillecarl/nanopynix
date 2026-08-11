"""Direct unit tests for pynix.target's evaluation-target helpers.

Real CLI integration tests (test_build.py, test_eval.py, test_flake_show.py)
only ever exercise evaluate_target/select_attr on their respective happy
paths. These dumb coverage tests pin down the auto_call_file=False branch,
the attrpath-with-empty-component guard, and evaluate_target's
already-validate()-guarded "no target" branch (kept as defense in depth, so
still worth pinning even though it's unreachable through the public
validate()-first call path).
"""

# ruff: noqa: ASYNC109
# The doubles below subclass real ValueProxy/EvalSession/ReplSession
# classes, whose async methods take a `timeout` keyword; an override has
# to keep it. Same exemption, same reason as
# nanopynix/rpc/client/_session.py, where the real signatures live.

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pynix.target import (
    EvaluationTarget,
    EvaluationTargetError,
    evaluate_target,
    resolve_file_reference,
    select_attr,
)

from nanopynix.rpc import EvalSession, ValueProxy
from nanopynix.settings import NixFlakeSettings


# These doubles subclass the real classes rather than duck-typing them, so
# that beartype's `isinstance` checks on annotated parameters accept them.
# Subclassing means their signatures have to *match*, hence the `timeout`
# keyword each one accepts and ignores: a double that quietly dropped an
# argument its caller passes would not be standing in for anything.
class _FakeValue(ValueProxy):
    def __init__(self, attrs: dict[str, _FakeValue] | None = None, *, auto_called: bool = False) -> None:
        self._attrs = attrs or {}
        self.auto_called = auto_called

    async def has_attr(self, name: str, *, timeout: float | None = None) -> bool:
        return name in self._attrs

    async def attr_names(self, *, timeout: float | None = None) -> list[str]:
        return list(self._attrs)

    def attr(self, name: str, *, timeout: float | None = None) -> _FakeValue:
        return self._attrs[name]

    async def auto_call(self, *, timeout: float | None = None) -> _FakeValue:
        return _FakeValue(self._attrs, auto_called=True)


class _FakeSession(EvalSession):
    def __init__(self, file_value: _FakeValue | None = None) -> None:
        self._file_value = file_value

    async def file(self, path: str, *, timeout: float | None = None) -> _FakeValue:
        if self._file_value is None:
            raise AssertionError("file() not expected")
        return self._file_value

    async def eval_flake(
        self,
        ref: str,
        *,
        write_lock_file: bool = True,
        flake_settings: NixFlakeSettings | None = None,
        timeout: float | None = None,
    ) -> _FakeValue:
        raise AssertionError("eval_flake() not expected")


async def test_evaluate_target_with_a_file_skips_auto_call_by_default() -> None:
    target = EvaluationTarget(file="x.nix", attr=None, flake=None)
    value = _FakeValue()
    session = _FakeSession(file_value=value)

    result = await evaluate_target(target, session)

    assert result is value
    assert not value.auto_called


async def test_evaluate_target_raises_if_flake_is_missing_after_bypassing_validate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """validate(required=True) always runs first in practice and would
    already raise "either --file or --flake is required" before this branch;
    bypass it (on the frozen dataclass's class, since instances can't be
    patched) to pin the defensive fallback directly."""

    def _no_op_validate(_self: EvaluationTarget, *, required: bool = False) -> None:
        del required

    target = EvaluationTarget(file=None, attr=None, flake=None)
    monkeypatch.setattr(EvaluationTarget, "validate", _no_op_validate)

    with pytest.raises(EvaluationTargetError, match="either --file or --flake is required"):
        await evaluate_target(target, _FakeSession())


async def test_select_attr_rejects_an_empty_attrpath_component() -> None:
    value: Any = _FakeValue({"a": _FakeValue()})

    with pytest.raises(EvaluationTargetError, match="empty component"):
        await select_attr(value, "a..b")


# --- resolve_file_reference -------------------------------------------------
#
# The six rules of the docstring, one test each, plus the two errors. The rule
# that decides an argument is not visible in the value that comes back, so each
# test names the rule it covers.


async def test_a_local_file_keeps_its_whole_argument(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 1: a name that exists is a path, '#' and all."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "weird#name.nix").write_text("1")

    reference = await resolve_file_reference("weird#name.nix")

    assert reference.argument == "weird#name.nix"
    assert reference.fragment is None
    assert reference.local_path == Path("weird#name.nix")


async def test_a_local_file_splits_its_fragment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 2: the part before the first '#' exists, so the rest is a fragment."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "default.nix").write_text("{ }")

    reference = await resolve_file_reference("default.nix#packages.hello")

    assert reference.argument == "default.nix"
    assert reference.fragment == "packages.hello"
    assert reference.local_path == Path("default.nix")


async def test_a_local_directory_wins_over_the_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 2 beats rule 6: 'nixpkgs' here is the directory, not the flake."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "nixpkgs").mkdir()

    reference = await resolve_file_reference("nixpkgs")

    assert reference.argument == "nixpkgs"
    assert reference.local_path == Path("nixpkgs")


@pytest.mark.parametrize("raw", ["./missing.nix", "../missing.nix", "/etc/missing.nix", "~/missing.nix"])
async def test_a_written_path_stays_a_path_when_it_is_absent(
    raw: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 3: the evaluator reports a missing file, and not a missing flake."""
    monkeypatch.chdir(tmp_path)

    reference = await resolve_file_reference(raw)

    assert reference.argument == raw
    assert reference.local_path is None


async def test_a_written_path_still_splits_its_fragment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    reference = await resolve_file_reference("./missing.nix#a.b")

    assert reference.argument == "./missing.nix"
    assert reference.fragment == "a.b"


@pytest.mark.parametrize(
    "raw",
    [
        "<nixpkgs>",
        "channel:nixos-unstable",
        "https://example.com/x.tar.gz",
        "flake:nixpkgs",
    ],
)
async def test_the_evaluator_keeps_what_it_resolves_itself(
    raw: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rules 4 and 5: lookup_file_arg has a branch for each of these."""
    monkeypatch.chdir(tmp_path)

    reference = await resolve_file_reference(raw)

    assert reference.argument == raw
    assert reference.local_path is None


async def test_a_pseudo_url_survives_its_double_slash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect that a `Path` annotation caused: 'https://' became 'https:/'."""
    monkeypatch.chdir(tmp_path)

    reference = await resolve_file_reference("https://example.com/x.tar.gz")

    assert reference.argument == "https://example.com/x.tar.gz"


@pytest.mark.parametrize(
    ("raw", "argument"),
    [
        ("github:NixOS/nixpkgs", "flake:github:NixOS/nixpkgs"),
        ("nixpkgs", "flake:nixpkgs"),
        ("nixpkgs/nixos-25.05", "flake:nixpkgs/nixos-25.05"),
        ("git+https://example.com/x", "flake:git+https://example.com/x"),
        ("path:/tmp/tree", "flake:path:/tmp/tree"),
    ],
)
async def test_a_reference_gets_the_flake_prefix(
    raw: str, argument: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 6: the branch of lookup_file_arg that fetches the tree."""
    monkeypatch.chdir(tmp_path)

    reference = await resolve_file_reference(raw)

    assert reference.argument == argument
    assert reference.fragment is None
    assert reference.local_path is None


async def test_a_reference_splits_its_fragment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    reference = await resolve_file_reference("github:NixOS/nixpkgs/nixos-25.05#lib.version")

    assert reference.argument == "flake:github:NixOS/nixpkgs/nixos-25.05"
    assert reference.fragment == "lib.version"


async def test_an_empty_fragment_is_no_fragment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    reference = await resolve_file_reference("nixpkgs#")

    assert reference.argument == "flake:nixpkgs"
    assert reference.fragment is None


async def test_an_empty_argument_is_an_error() -> None:
    with pytest.raises(EvaluationTargetError, match="needs a value"):
        await resolve_file_reference("")


async def test_a_bare_fragment_is_an_error() -> None:
    with pytest.raises(EvaluationTargetError, match="no reference before"):
        await resolve_file_reference("#lib.version")


async def test_standard_input_is_refused_by_name() -> None:
    """'-f -' reads an expression from stdin in Nix, and pynix does not yet."""
    with pytest.raises(EvaluationTargetError, match="standard input"):
        await resolve_file_reference("-")


async def test_the_target_resolves_its_own_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "default.nix").write_text("{ }")

    target = EvaluationTarget(file="default.nix#a", attr=None, flake=None)
    reference = await target.file_reference()

    assert reference is not None
    assert reference.fragment == "a"
    assert await EvaluationTarget(file=None, attr=None, flake="x").file_reference() is None


async def test_a_fragment_selects_after_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fragment reaches select_attr, and --attr still applies on top."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "default.nix").write_text("{ }")
    leaf = _FakeValue()
    root = _FakeValue({"a": _FakeValue({"b": leaf})})

    target = EvaluationTarget(file="default.nix#a", attr="b", flake=None)
    result = await evaluate_target(target, _FakeSession(file_value=root))

    assert result is leaf
