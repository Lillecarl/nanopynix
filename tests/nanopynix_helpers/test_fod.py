"""Tests for fixed-output derivation source updates."""

from __future__ import annotations

import pytest
from nanopynix_helpers.fod import (
    FodHashMismatch,
    FodSourceUpdateError,
    derivation_name_from_path,
    evaluated_derivation_path,
    extract_fod_hash_mismatch,
    extract_unique_fod_hash_mismatch,
    find_fod_hash_literal,
    fixed_output_derivations_in_closure,
    is_fixed_output_derivation_in_closure,
    mismatch_is_target_fod,
    replace_fod_hash,
)
from nanopynix_proto.nix.common import Derivation, DerivationOutput, DerivationOutputs


def test_extracts_only_the_exact_ansi_colored_nix_fod_shape() -> None:
    mismatch = extract_fod_hash_mismatch(
        "error: hash mismatch in fixed-output derivation '\x1b[35;1m/nix/store/source.drv\x1b[0m':\n"
        "  specified: \x1b[35;1msha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\x1b[0m\n"
        "     got:    \x1b[35;1msha256-XG19bBLOoknhsnwV5rVaVGB8DYUiNPMklhyNotZNcD4=\x1b[0m",
    )

    assert mismatch is not None
    assert mismatch.got == "sha256-XG19bBLOoknhsnwV5rVaVGB8DYUiNPMklhyNotZNcD4="
    assert mismatch.drv_path == "/nix/store/source.drv"
    assert extract_fod_hash_mismatch("hash mismatch in an unrelated format") is None


def test_extract_unique_fod_hash_mismatch_rejects_multiple_events() -> None:
    first = (
        "error: hash mismatch in fixed-output derivation '/nix/store/a.drv':\n"
        "  specified: sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n"
        "  got: sha256-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB="
    )
    second = first.replace("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC")

    assert extract_unique_fod_hash_mismatch([first]) == extract_fod_hash_mismatch(first)
    assert extract_unique_fod_hash_mismatch([first, second]) is None


def test_replaces_one_plain_hash_literal() -> None:
    source = 'pkgs.fetchFromGitHub { owner = "lillecarl"; hash = ""; }\n'
    literal = find_fod_hash_literal(source, "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

    assert replace_fod_hash(source, literal, "sha256-XG19bBLOoknhsnwV5rVaVGB8DYUiNPMklhyNotZNcD4=") == (
        'pkgs.fetchFromGitHub { owner = "lillecarl"; hash = "sha256-XG19bBLOoknhsnwV5rVaVGB8DYUiNPMklhyNotZNcD4="; }\n'
    )


def test_refuses_ambiguous_hash_literals() -> None:
    source = '{ first = { hash = ""; }; second = { sha256 = ""; }; }\n'

    with pytest.raises(FodSourceUpdateError, match="multiple hash literals"):
        find_fod_hash_literal(source, "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")


def test_matches_run_command_output_hash_by_derivation_name() -> None:
    source = """with import <nixpkgs> {};
let
  first = runCommand "first" { outputHash = ""; outputHashAlgo = "sha256"; outputHashMode = "flat"; } "echo first > $out";
  second = runCommand "second" { outputHash = ""; outputHashAlgo = "sha256"; outputHashMode = "flat"; } "echo second > $out";
in [ first second ]
"""

    literal = find_fod_hash_literal(
        source,
        "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        derivation_name="second",
    )

    assert literal.derivation_name == "second"
    assert (
        replace_fod_hash(source, literal, "sha256-XG19bBLOoknhsnwV5rVaVGB8DYUiNPMklhyNotZNcD4=").count("sha256-") == 1
    )


def test_find_fod_hash_literal_prefers_an_exact_value_match_over_ambiguity() -> None:
    """Two candidates exist, but one's stored value already equals *specified*
    (e.g. it was never actually empty); that one must win outright rather than
    falling through to the "multiple hash literals" ambiguity error."""
    source = (
        'first = { hash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="; }; second = { hash = "sha256-BBBB"; };'
    )

    literal = find_fod_hash_literal(source, "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

    assert literal.value == "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def test_find_fod_hash_literal_raises_when_derivation_name_matches_nothing() -> None:
    """A derivation_name hint that matches zero (or more than one) candidate
    must fall through to the plain candidate-count check, not silently pick
    one -- here that count is itself ambiguous."""
    source = 'first = { hash = ""; }; second = { hash = ""; };'

    with pytest.raises(FodSourceUpdateError, match="multiple hash literals"):
        find_fod_hash_literal(source, "sha256-nomatch", derivation_name="nonexistent")


def test_find_fod_hash_literal_raises_when_no_candidates_exist() -> None:
    source = '{ pname = "nothing-hash-shaped-here"; }'

    with pytest.raises(FodSourceUpdateError, match="no plain hash"):
        find_fod_hash_literal(source, "sha256-nomatch")


def test_replace_fod_hash_rejects_a_computed_hash_with_unsafe_characters() -> None:
    source = 'x = { hash = ""; };'
    literal = find_fod_hash_literal(source, "sha256-nomatch")

    with pytest.raises(FodSourceUpdateError, match="not safe to insert"):
        replace_fod_hash(source, literal, 'sha256-has"quote')
    with pytest.raises(FodSourceUpdateError, match="not safe to insert"):
        replace_fod_hash(source, literal, "sha256-has\nnewline")


def test_find_fod_hash_literal_skips_a_non_runcommand_curried_call() -> None:
    """_run_command_hash_bindings must not attribute a derivation name to a
    curried call whose callee isn't runCommand, even though it has the same
    "NAME { attrs }" apply shape."""
    source = """
    let
      first = mkDerivation "first" { outputHash = ""; };
      second = runCommand "second" { outputHash = ""; } "cmd";
    in [ first second ]
    """

    literal = find_fod_hash_literal(source, "sha256-nomatch", derivation_name="second")

    assert literal.derivation_name == "second"


def test_find_fod_hash_literal_skips_inherit_statements_in_a_runcommand_binding_set() -> None:
    source = """
    let
      second = runCommand "second" { inherit somethingUnrelated; outputHash = ""; } "cmd";
    in second
    """

    literal = find_fod_hash_literal(source, "sha256-nomatch", derivation_name="second")

    assert literal.derivation_name == "second"


def test_derivation_name_from_path_handles_missing_and_malformed_paths() -> None:
    assert derivation_name_from_path(None) is None
    assert derivation_name_from_path("/nix/store/no-drv-suffix") is None
    assert derivation_name_from_path("/nix/store/nodash.drv") is None
    assert derivation_name_from_path("/nix/store/abc123hash-real-name.drv") == "real-name"


class _FakeForcedValue:
    def __init__(self, value: object) -> None:
        self._value = value

    async def force_json(self) -> object:
        return self._value


class _FakeEvalValue:
    """Duck-types the subset of ValueProxy that evaluated_derivation_path needs."""

    def __init__(self, attrs: dict[str, object] | None = None) -> None:
        self._attrs = attrs or {}

    async def has_attr(self, name: str) -> bool:
        return name in self._attrs

    def attr(self, name: str) -> _FakeForcedValue:
        return _FakeForcedValue(self._attrs[name])


async def test_evaluated_derivation_path_returns_none_without_a_type_attr() -> None:
    assert await evaluated_derivation_path(_FakeEvalValue()) is None  # type: ignore[arg-type] -- narrow ValueProxy fake


async def test_evaluated_derivation_path_returns_none_for_a_non_derivation_type() -> None:
    value = _FakeEvalValue({"type": "package"})

    assert await evaluated_derivation_path(value) is None  # type: ignore[arg-type] -- narrow ValueProxy fake


async def test_evaluated_derivation_path_returns_none_without_a_drvpath() -> None:
    value = _FakeEvalValue({"type": "derivation"})

    assert await evaluated_derivation_path(value) is None  # type: ignore[arg-type] -- narrow ValueProxy fake


async def test_evaluated_derivation_path_rejects_a_non_string_drvpath() -> None:
    value = _FakeEvalValue({"type": "derivation", "drvPath": 123})

    with pytest.raises(FodSourceUpdateError, match="drvPath was not a string"):
        await evaluated_derivation_path(value)  # type: ignore[arg-type] -- narrow ValueProxy fake


class _FakeStore:
    """Duck-types the subset of Store that these closure walks need."""

    def __init__(self, derivations: dict[str, Derivation]) -> None:
        self._derivations = derivations

    async def read_derivation(self, drv_path: str) -> Derivation:
        return self._derivations[drv_path]


async def test_fixed_output_derivations_in_closure_walks_input_drvs_once_each() -> None:
    root = "/nix/store/root.drv"
    fixed_dep = "/nix/store/fixed-dep.drv"
    plain_dep = "/nix/store/plain-dep.drv"
    derivations = {
        root: Derivation(
            name="root",
            input_drvs={
                fixed_dep: DerivationOutputs(outputs=["out"]),
                plain_dep: DerivationOutputs(outputs=["out"]),
            },
            outputs={"out": DerivationOutput(type="Regular")},
        ),
        fixed_dep: Derivation(name="fixed-dep", input_drvs={}, outputs={"out": DerivationOutput(type="CAFixed")}),
        # Points back to root -- exercises the visited-set cycle guard.
        plain_dep: Derivation(
            name="plain-dep",
            input_drvs={root: DerivationOutputs(outputs=["out"])},
            outputs={"out": DerivationOutput(type="Regular")},
        ),
    }
    store = _FakeStore(derivations)

    result = await fixed_output_derivations_in_closure(store, root)  # type: ignore[arg-type] -- narrow store fake

    assert result == {fixed_dep}


async def test_mismatch_is_target_fod_is_true_without_a_drv_path() -> None:
    mismatch = FodHashMismatch(specified="a", got="b", drv_path=None)

    assert await mismatch_is_target_fod(None, None, mismatch)  # type: ignore[arg-type] -- store/root unused when drv_path is None


async def test_mismatch_is_target_fod_is_false_when_root_is_not_a_derivation() -> None:
    mismatch = FodHashMismatch(specified="a", got="b", drv_path="/nix/store/x.drv")

    assert not await mismatch_is_target_fod(None, _FakeEvalValue(), mismatch)  # type: ignore[arg-type] -- narrow fakes


async def test_is_fixed_output_derivation_in_closure_true_via_matching_derivation_name() -> None:
    root = "/nix/store/root.drv"
    dep = "/nix/store/oldhash-dep.drv"
    candidate = "/nix/store/newhash-dep.drv"
    derivations = {
        root: Derivation(
            name="root",
            input_drvs={dep: DerivationOutputs(outputs=["out"])},
            outputs={"out": DerivationOutput(type="Regular")},
        ),
        dep: Derivation(name="dep", input_drvs={}, outputs={"out": DerivationOutput(type="CAFixed")}),
    }
    store = _FakeStore(derivations)

    assert await is_fixed_output_derivation_in_closure(store, root, candidate)  # type: ignore[arg-type] -- narrow store fake


async def test_is_fixed_output_derivation_in_closure_false_when_name_match_is_not_fixed_output() -> None:
    root = "/nix/store/root.drv"
    dep = "/nix/store/oldhash-dep.drv"
    candidate = "/nix/store/newhash-dep.drv"
    derivations = {
        root: Derivation(
            name="root",
            input_drvs={dep: DerivationOutputs(outputs=["out"])},
            outputs={"out": DerivationOutput(type="Regular")},
        ),
        dep: Derivation(name="dep", input_drvs={}, outputs={"out": DerivationOutput(type="Regular")}),
    }
    store = _FakeStore(derivations)

    assert not await is_fixed_output_derivation_in_closure(store, root, candidate)  # type: ignore[arg-type] -- narrow store fake


async def test_is_fixed_output_derivation_in_closure_false_when_name_match_is_ambiguous() -> None:
    root = "/nix/store/root.drv"
    dep1 = "/nix/store/hash1-dep.drv"
    dep2 = "/nix/store/hash2-dep.drv"
    candidate = "/nix/store/newhash-dep.drv"
    derivations = {
        root: Derivation(
            name="root",
            input_drvs={dep1: DerivationOutputs(outputs=["out"]), dep2: DerivationOutputs(outputs=["out"])},
            outputs={"out": DerivationOutput(type="Regular")},
        ),
        dep1: Derivation(name="dep", input_drvs={}, outputs={"out": DerivationOutput(type="CAFixed")}),
        dep2: Derivation(name="dep", input_drvs={}, outputs={"out": DerivationOutput(type="CAFixed")}),
    }
    store = _FakeStore(derivations)

    assert not await is_fixed_output_derivation_in_closure(store, root, candidate)  # type: ignore[arg-type] -- narrow store fake
