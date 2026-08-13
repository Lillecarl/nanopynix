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
    FileReference,
    app_attr_search,
    base_attr_search,
    dev_shell_attr_search,
    evaluate_target,
    formatter_attr_search,
    open_file_reference,
    repl_attr_search,
    resolve_file_reference,
    select_attr,
)

import nanopynix
from nanopynix.exceptions import ThrownError
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

    assert reference.arguments == ("weird#name.nix",)
    assert reference.fragment is None
    assert reference.local_path == Path("weird#name.nix")


async def test_a_local_file_splits_its_fragment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 2: the part before the first '#' exists, so the rest is a fragment."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "default.nix").write_text("{ }")

    reference = await resolve_file_reference("default.nix#packages.hello")

    assert reference.arguments == ("default.nix",)
    assert reference.fragment == "packages.hello"
    assert reference.local_path == Path("default.nix")


async def test_a_local_directory_wins_over_the_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 2 beats rule 6: 'nixpkgs' here is the directory, not the flake."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "nixpkgs").mkdir()

    reference = await resolve_file_reference("nixpkgs")

    assert reference.arguments == ("nixpkgs",)
    assert reference.local_path == Path("nixpkgs")


@pytest.mark.parametrize("raw", ["./missing.nix", "../missing.nix", "/etc/missing.nix", "~/missing.nix"])
async def test_a_written_path_stays_a_path_when_it_is_absent(
    raw: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 3: the evaluator reports a missing file, and not a missing flake."""
    monkeypatch.chdir(tmp_path)

    reference = await resolve_file_reference(raw)

    assert reference.arguments == (raw,)
    assert reference.local_path is None


async def test_a_written_path_still_splits_its_fragment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    reference = await resolve_file_reference("./missing.nix#a.b")

    assert reference.arguments == ("./missing.nix",)
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

    assert reference.arguments == (raw,)
    assert reference.local_path is None


async def test_a_pseudo_url_survives_its_double_slash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect that a `Path` annotation caused: 'https://' became 'https:/'."""
    monkeypatch.chdir(tmp_path)

    reference = await resolve_file_reference("https://example.com/x.tar.gz")

    assert reference.arguments == ("https://example.com/x.tar.gz",)


@pytest.mark.parametrize(
    ("raw", "argument"),
    [
        ("github:NixOS/nixpkgs", "flake:github:NixOS/nixpkgs"),
        ("git+https://example.com/x", "flake:git+https://example.com/x"),
        ("path:/tmp/tree", "flake:path:/tmp/tree"),
    ],
)
async def test_a_reference_that_carries_a_scheme_goes_straight_to_the_registry(
    raw: str, argument: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 6: the lookup path never holds a name such as `github:owner/repo`."""
    monkeypatch.chdir(tmp_path)

    reference = await resolve_file_reference(raw)

    assert reference.arguments == (argument,)
    assert reference.fragment is None
    assert reference.local_path is None


@pytest.mark.parametrize("raw", ["nixpkgs", "nixpkgs/nixos-25.05"])
async def test_a_bare_name_asks_the_lookup_path_before_the_registry(
    raw: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 7: `--file` is the old-style door, and NIX_PATH is how a name became
    a tree before flakes existed."""
    monkeypatch.chdir(tmp_path)

    reference = await resolve_file_reference(raw)

    assert reference.arguments == (f"<{raw}>", f"flake:{raw}")
    assert reference.local_path is None


async def test_a_reference_splits_its_fragment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    reference = await resolve_file_reference("github:NixOS/nixpkgs/nixos-25.05#lib.version")

    assert reference.arguments == ("flake:github:NixOS/nixpkgs/nixos-25.05",)
    assert reference.fragment == "lib.version"


async def test_an_empty_fragment_is_no_fragment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    reference = await resolve_file_reference("nixpkgs#")

    assert reference.arguments == ("<nixpkgs>", "flake:nixpkgs")
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


# --- the attribute-path search of each command ------------------------------
#
# One test for each command that copies a `nix` subcommand. The lists that
# `nix` uses are in `src/libcmd/installables.cc`, `src/nix/develop.cc`,
# `src/nix/run.cc` and `src/nix/formatter.cc`, and each test names the order
# that the matching file decides.


def test_the_base_search_matches_source_expr_command() -> None:
    system = nanopynix.current_system()
    search = base_attr_search()

    assert search.prefixes == (f"packages.{system}.", f"legacyPackages.{system}.")
    assert search.defaults == (f"packages.{system}.default", f"defaultPackage.{system}")


def test_the_dev_shell_search_puts_dev_shells_in_front() -> None:
    system = nanopynix.current_system()
    search = dev_shell_attr_search()

    assert search.prefixes == (
        f"devShells.{system}.",
        f"packages.{system}.",
        f"legacyPackages.{system}.",
    )
    assert search.defaults == (
        f"devShells.{system}.default",
        f"devShell.{system}",
        f"packages.{system}.default",
        f"defaultPackage.{system}",
    )


def test_the_app_search_puts_apps_in_front() -> None:
    system = nanopynix.current_system()
    search = app_attr_search()

    assert search.prefixes[0] == f"apps.{system}."
    assert search.prefixes[1:] == base_attr_search().prefixes
    assert search.defaults[:2] == (f"apps.{system}.default", f"defaultApp.{system}")
    assert search.defaults[2:] == base_attr_search().defaults


def test_the_formatter_search_has_no_prefix() -> None:
    system = nanopynix.current_system()
    search = formatter_attr_search()

    assert search.prefixes == ()
    assert search.defaults == (f"formatter.{system}",)


def test_the_repl_search_defaults_to_the_root() -> None:
    """`CmdRepl` overrides its defaults to one empty path, and keeps the prefixes.

    An empty path selects the outputs themselves, so `pynix repl --flake <ref>`
    puts every output into scope. The prefixes still apply to a fragment.
    """
    search = repl_attr_search()

    assert search.defaults == ("",)
    assert search.prefixes == base_attr_search().prefixes
    assert search.candidates(None) == ("",)
    assert search.candidates("hello")[-1] == "hello"


# --- open_file_reference ----------------------------------------------------
#
# The candidate list of a bare name reaches the evaluator one at a time, and
# only a miss in the lookup path moves to the next one. These tests drive the
# opener with a double, so each branch is reachable without a lookup path that
# the test machine happens to hold.


def _search_path_miss(name: str) -> ThrownError:
    """The error `EvalState::findFile` raises when the lookup path has no *name*."""
    return ThrownError(
        "ThrownError",
        f"error: file '{name}' was not found in the Nix search path (add it using $NIX_PATH or -I)",
    )


class _RecordingOpener:
    """An opener that answers from a table, and remembers what it was asked."""

    def __init__(self, answers: dict[str, _FakeValue | Exception]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    async def __call__(self, candidate: str) -> _FakeValue:
        self.asked.append(candidate)
        answer = self.answers.get(candidate)
        if answer is None:
            raise _search_path_miss(candidate.strip("<>"))
        if isinstance(answer, Exception):
            raise answer
        return answer


async def test_the_lookup_path_answers_before_the_registry() -> None:
    value = _FakeValue()
    opener = _RecordingOpener({"<nixpkgs>": value})
    reference = FileReference(arguments=("<nixpkgs>", "flake:nixpkgs"), fragment=None, local_path=None)

    assert await open_file_reference(reference, opener) is value
    assert opener.asked == ["<nixpkgs>"]


async def test_the_registry_answers_when_the_lookup_path_has_no_such_name() -> None:
    value = _FakeValue()
    opener = _RecordingOpener({"flake:nixpkgs": value})
    reference = FileReference(arguments=("<nixpkgs>", "flake:nixpkgs"), fragment=None, local_path=None)

    assert await open_file_reference(reference, opener) is value
    assert opener.asked == ["<nixpkgs>", "flake:nixpkgs"]


async def test_a_found_name_that_fails_to_evaluate_keeps_its_own_error() -> None:
    """The reason the class alone is not enough to decide the fallback.

    `builtins.throw` raises `ThrownError` too, so a `<nixpkgs>` that the lookup
    path holds and whose file then rejects its arguments must report that, and
    not a message about a flake.
    """
    opener = _RecordingOpener({"<nixpkgs>": ThrownError("ThrownError", "error: this expression refuses to evaluate")})
    reference = FileReference(arguments=("<nixpkgs>", "flake:nixpkgs"), fragment=None, local_path=None)

    with pytest.raises(ThrownError, match="refuses to evaluate"):
        await open_file_reference(reference, opener)

    assert opener.asked == ["<nixpkgs>"]


async def test_the_last_error_survives_when_no_candidate_answers() -> None:
    opener = _RecordingOpener({})
    reference = FileReference(arguments=("<nixpkgs>", "flake:nixpkgs"), fragment=None, local_path=None)

    with pytest.raises(ThrownError, match="Nix search path"):
        await open_file_reference(reference, opener)

    assert opener.asked == ["<nixpkgs>", "flake:nixpkgs"]
