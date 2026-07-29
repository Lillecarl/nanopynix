"""How a Nix setting reaches Nix, and what happens when it cannot.

Nix reads a setting at one of four moments: process start, store construction,
evaluator construction, or fresh at the point of use. A setting applied after
the moment Nix reads it is accepted, stored, and never looked at again.

Each test below pins one of those moments. The ``configure`` tests all assert
the same thing from different angles: the silent drop is gone. Every one of
them passed before this behaviour existed, by measuring the drop; they now
prove the raise, and the recorded before-state is quoted in each docstring.

Both engines are checked wherever the answer could differ, because an
inproc/rpc asymmetry is a defect unless process isolation forces it.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# nanopynix types are C++ nanobind extensions without type stubs.

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pytest_agent import note

import nanopynix
from nanopynix import NixEvalSettings, NixFetchSettings, stores
from nanopynix.exceptions import EvalError, SettingNotLiveError
from nanopynix.namespace import STORE_DIR
from nanopynix.settings import (
    NixEvaluatorSettings,
    construction_time_keys,
    field_is_supported,
    reject_construction_time_keys,
)

if TYPE_CHECKING:
    from tests.support.nix_environment import NixTestEnvironment


#: The eight eval settings Nix reads while it builds the evaluator. Restated
#: here on purpose: if the model changes, this list is what notices.
CONSTRUCTION_TIME_EVAL_SETTINGS = frozenset(
    {
        "eval-profile-file",
        "eval-profiler",
        "eval-profiler-frequency",
        "eval-system",
        "nix-path",
        "pure-eval",
        "restrict-eval",
        "trace-function-calls",
    },
)


def _host_bash() -> str:
    """The store path of a bash already in the host store, for use as a builder."""
    resolved = shutil.which("bash")
    if resolved is None:
        pytest.skip("no bash on PATH to use as a builder")
    real = Path(resolved).resolve()
    for parent in real.parents:
        if parent.parent == Path(STORE_DIR):
            return str(parent)
    pytest.skip(f"bash at {real} is not in {STORE_DIR}, so it cannot be a builder here")


def _derivation_text(name: str, bash: str) -> str:
    return (
        f'let bash = builtins.storePath "{bash}";\n'
        f"in derivation {{\n"
        f'  name = "{name}";\n'
        f"  system = builtins.currentSystem;\n"
        f'  builder = "${{bash}}/bin/bash";\n'
        f'  args = [ "-c" "echo config-flow > $out" ];\n'
        f"}}\n"
    )


# ── The model records when Nix reads each setting ────────────────────


def test_the_model_names_exactly_the_construction_time_eval_settings() -> None:
    """The tags are the source of truth for every raise, so they are pinned.

    A wrong tag reinstates the bug this whole area exists to remove: tagging a
    construction-time setting live makes it silently ignored again.
    """
    tagged = construction_time_keys(NixEvalSettings)
    note(construction_time=sorted(tagged))
    assert tagged == CONSTRUCTION_TIME_EVAL_SETTINGS


def test_every_fetch_setting_is_live() -> None:
    """``EvalState`` holds a reference to the fetcher settings, not a copy.

    ``eval.hh`` says "Must outlive the lifetime of this EvalState!" of that
    reference. Nothing snapshots it, so nothing can go stale.
    """
    assert construction_time_keys(NixFetchSettings) == frozenset()


def test_the_evaluator_catch_all_inherits_both_scopes() -> None:
    """One object configures an evaluator, and narrow parameters still take it."""
    # One field from each scope. `warn_dirty` rather than `tarball_ttl`,
    # because every supported Nix keeps `warn-dirty` in the fetcher registry.
    settings = NixEvaluatorSettings(max_call_depth=20, warn_dirty=False)
    assert isinstance(settings, NixEvalSettings)
    assert isinstance(settings, NixFetchSettings)
    assert settings.to_worker_settings() == {"max-call-depth": "20", "warn-dirty": "false"}


# ── configure(): the raise that replaced the silent drop ─────────────


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pure_eval", True),
        ("nix_path", ["spike=/tmp"]),
        ("restrict_eval", True),
        ("eval_system", "x86_64-linux"),
        ("trace_function_calls", True),
    ],
)
async def test_rpc_configure_refuses_a_construction_time_setting(
    shared_nix_environment: NixTestEnvironment,
    field: str,
    value: Any,
) -> None:
    """Each of these used to be accepted and dropped.

    Measured before the change: ``configure(pure_eval=True)`` left
    ``builtins.currentTime`` working, ``configure(nix_path=...)`` never reached
    Nix at all, and ``configure(restrict_eval=True)`` did not stop ``readFile``
    of a path outside the store. All three reported success.
    """
    async with (
        shared_nix_environment.rpc_session() as session,
        session.store() as store,
        session.eval(store) as evaluator,
    ):
        with pytest.raises(SettingNotLiveError) as excinfo:
            await evaluator.configure(NixEvalSettings(**{field: value}))
        note(**{f"rpc/{field}": str(excinfo.value)})
        assert field.replace("_", "-") in str(excinfo.value), "the message must name the setting"
        assert "open the evaluator" in str(excinfo.value), "the message must say what to do instead"


@pytest.mark.parametrize(("field", "value"), [("pure_eval", True), ("nix_path", ["spike=/tmp"])])
async def test_inproc_configure_refuses_the_same_settings(
    shared_nix_environment: NixTestEnvironment,
    field: str,
    value: Any,
) -> None:
    """The in-process engine refuses exactly what the RPC engine refuses."""
    async with (
        shared_nix_environment.inproc_session() as session,
        session.store() as store,
        session.eval(store) as evaluator,
    ):
        with pytest.raises(SettingNotLiveError):
            await evaluator.configure(NixEvalSettings(**{field: value}))


async def test_configure_still_applies_a_live_setting(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """The raise is targeted, not a blanket refusal.

    ``max-call-depth`` is read at call time and never in the constructor, so
    lowering it takes effect on the next evaluation.
    """
    recurse = "let f = n: if n == 0 then 0 else f (n - 1); in f 200"

    async with (
        shared_nix_environment.rpc_session() as session,
        session.store() as store,
        session.eval(store) as evaluator,
    ):
        baseline = await (await evaluator.string(recurse)).as_int()
        note(recursion_before_configure=baseline)
        assert baseline == 0

        await evaluator.configure(NixEvalSettings(max_call_depth=20))

        with pytest.raises(EvalError, match="max-call-depth"):
            await evaluator.string(recurse)


async def test_configure_accepts_every_fetch_setting(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """No fetch setting is refused, because none of them is snapshotted."""
    # `tarball-ttl` is a global on Nix 2.31 and a fetch setting from 2.34, so
    # the field carries a version gate and this test has to respect it.
    values: dict[str, Any] = {"warn_dirty": False}
    if field_is_supported(NixFetchSettings.model_fields["tarball_ttl"]):
        values["tarball_ttl"] = 1
    note(fetch_settings=sorted(values))

    async with (
        shared_nix_environment.rpc_session() as session,
        session.store() as store,
        session.eval(store) as evaluator,
    ):
        await evaluator.configure(fetch_settings=NixFetchSettings(**values))


async def test_a_construction_time_setting_works_when_the_evaluator_opens(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
) -> None:
    """The raise points at a route that works, so that route is checked too."""
    target = tmp_path / "config-flow"
    target.mkdir()
    (target / "default.nix").write_text("42\n")

    async with (
        shared_nix_environment.rpc_session() as session,
        session.store() as store,
        session.eval(store, eval_settings=NixEvalSettings(nix_path=[f"cfgflow={target}"])) as evaluator,
    ):
        value = await (await evaluator.string("import <cfgflow>")).as_int()
        assert value == 42


async def test_both_engines_honour_a_per_evaluator_search_path(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
) -> None:
    """One evaluator's ``nix_path`` must not leak from, or into, the session's.

    This is the test that found the asymmetry it now guards. The in-process
    engine always honoured a per-evaluator search path. The RPC client dropped
    it before sending, and the worker used the session's, so the same code gave
    two answers depending on the engine. Nothing about process isolation forces
    that, so the search path now travels in its own field of ``OpenEval``.
    """
    first = tmp_path / "first"
    second = tmp_path / "second"
    for index, directory in enumerate((first, second), start=1):
        directory.mkdir()
        (directory / "default.nix").write_text(f"{index}\n")

    for engine, factory in (
        ("inproc", shared_nix_environment.inproc_session),
        ("rpc", shared_nix_environment.rpc_session),
    ):
        seen = await _resolve_search_paths(factory, [f"parity={first}", f"parity={second}"])
        note(**{f"{engine}/search_path": seen})
        assert seen == [1, 2], f"{engine} did not honour the per-evaluator search path"


async def _resolve_search_paths(factory: Any, entries: list[str]) -> list[int]:
    """Open one evaluator per entry, and read what each of them resolves.

    ``factory`` is untyped because the two engines have distinct ``Store`` and
    ``EvalSession`` classes with no common nominal base. The point of the test
    is that both answer the same, so it is written against neither.
    """
    resolved: list[int] = []
    async with factory() as session, session.store() as store:
        for entry in entries:
            async with session.eval(store, eval_settings=NixEvalSettings(nix_path=[entry])) as evaluator:
                resolved.append(await (await evaluator.string("import <parity>")).as_int())
    return resolved


# ── The worker refuses what the client refuses ───────────────────────


def test_the_worker_side_check_works_on_rendered_keys() -> None:
    """A request built by hand cannot get past the client-side check.

    Both sides run this one check, on the rendered keys, because a rendered
    mapping is all that reaches a worker. A hand-built request meets the same
    refusal the client would have raised.
    """
    with pytest.raises(SettingNotLiveError, match="pure-eval"):
        reject_construction_time_keys({"pure-eval": "true"}, model=NixEvalSettings, target="evaluator")

    reject_construction_time_keys({"max-call-depth": "20"}, model=NixEvalSettings, target="evaluator")


def test_the_message_names_every_offending_setting_at_once() -> None:
    """One call reports all of its problems, rather than one per attempt."""
    with pytest.raises(SettingNotLiveError) as excinfo:
        reject_construction_time_keys(
            {"pure-eval": "true", "nix-path": "x=/tmp", "max-call-depth": "20"},
            model=NixEvalSettings,
            target="evaluator",
        )
    message = str(excinfo.value)
    note(combined_message=message)
    assert "nix-path" in message
    assert "pure-eval" in message
    assert "max-call-depth" not in message, "a live setting must not be blamed"


# ── Store construction is its own moment ─────────────────────────────


async def test_a_store_setting_in_the_uri_beats_the_global(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
) -> None:
    """``require-sigs`` on the store is what decides, not the process global.

    Measured: with the global ``require-sigs`` left at true, copying an
    unsigned path failed into a store opened without the parameter and
    succeeded into one opened with ``require-sigs=false`` in its URI. A store
    reads its settings once, when it is constructed, so turning the global off
    afterwards changed nothing for a store already open.
    """
    name = f"nanopynix-cfg-flow-{uuid.uuid4().hex[:12]}"
    nix_file = tmp_path / "drv.nix"
    nix_file.write_text(_derivation_text(name, _host_bash()), encoding="utf-8")

    async with (
        shared_nix_environment.rpc_session() as session,
        session.store() as source,
    ):
        async with session.eval(source) as evaluator:
            outputs = await (await evaluator.file(str(nix_file))).build()
        built = next(iter(outputs.values()))
        note(built_path=built)

        strict = stores.Local(root=str(tmp_path / "strict"))
        async with session.store(strict) as destination:
            with pytest.raises(Exception, match="signature") as excinfo:
                await source.copy_closure([built], destination, check_sigs=True)
            note(copy_into_strict_store=str(excinfo.value)[:120])

        relaxed = stores.Local(root=str(tmp_path / "relaxed"), require_sigs=False)
        async with session.store(relaxed) as destination:
            await source.copy_closure([built], destination, check_sigs=True)
            note(copy_into_relaxed_store="SUCCEEDED")


async def test_a_store_model_opens_the_same_store_as_its_uri(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
) -> None:
    """``session.store()`` takes a model and a URI, and they agree."""
    config = stores.Local(root=str(tmp_path / "twin"), require_sigs=False)

    async with shared_nix_environment.rpc_session() as session:
        async with session.store(config) as from_model:
            model_dir = await from_model.store_dir()
        async with session.store(config.uri()) as from_uri:
            uri_dir = await from_uri.store_dir()

    assert model_dir == uri_dir


# ── Provenance: which values this session replaced ───────────────────


async def test_the_session_reports_what_it_took_from_the_host_config(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """With ``load_config=False`` nothing comes from the host, and that shows.

    The suite runs with the host configuration off, so ``from_config`` is
    empty here. The assertion that matters is the shape: an applied setting is
    reported as applied, which is what makes an override visible at all.
    """
    async with shared_nix_environment.rpc_session() as session, session.store() as store:
        await store.store_dir()

    provenance = nanopynix.SettingsProvenance(
        from_config={"max-jobs": "99"},
        applied={"max-jobs": "4", "cores": "2"},
    )
    note(overridden_from_config=provenance.overridden_from_config)
    assert provenance.overridden_from_config == {"max-jobs": "4"}


# ── Session-scoped globals, on both engines ──────────────────────────


def _open_session(environment: NixTestEnvironment, engine: str, **overrides: Any) -> Any:
    """Open one session of ``engine``, with the shared settings plus ``overrides``.

    The environment's own factories bind ``settings`` themselves, so a test that
    needs a different value has to build the session directly. The return type
    is ``Any`` because the two engines share no nominal base -- which is the
    thing these tests check.
    """
    session_class = nanopynix.rpc.Session if engine == "rpc" else nanopynix.inproc.Session
    return session_class(
        store_uri=environment.store_uri,
        load_config=False,
        settings=environment.settings.model_copy(update=overrides),
    )


@pytest.mark.parametrize("engine", ["inproc", "rpc"])
async def test_both_engines_read_and_write_the_session_settings(
    shared_nix_environment: NixTestEnvironment,
    engine: str,
) -> None:
    """Reading and writing the globals is a session operation on both engines.

    It has to be. For RPC the globals live in the worker's own process, so the
    module-level ``set_setting`` this replaces mutated the manager's copy and
    the worker never learned. Run 833 measured the asymmetry directly: with a
    store opened *after* the change, inproc SUCCEEDED and rpc RAISED. Same
    call, two answers, and the only variable was the process boundary.
    """
    # The settings are the environment's own, unchanged. One process hosts at
    # most one inproc session, and a second one with different settings is
    # refused -- which is the same "already constructed from the globals"
    # argument this test is about, one level up.
    expected_substituters = " ".join(shared_nix_environment.settings.substituters or ())

    seen: dict[str, Any] = {}
    async with _open_session(shared_nix_environment, engine) as session:
        overridden = await session.settings(overridden_only=True)
        seen["from_session"] = overridden.get("substituters")
        seen["provenance"] = (await session.settings_provenance()).applied.get("substituters")

        seen["written"] = await session.set_settings(nanopynix.NixGlobalSettings(max_jobs=9, keep_going=True))
        seen["read_back"] = (await session.settings(overridden_only=True)).get("max-jobs")
        # The unfiltered read reports every setting, not only the overridden.
        seen["read_sizes"] = (len(overridden), len(await session.settings()))

    note(**{f"{engine}/settings": {key: str(value)[:70] for key, value in seen.items()}})

    assert seen["from_session"] == expected_substituters, "the session's own setting must read back as applied"
    assert seen["provenance"] == expected_substituters, "and be reported as ours rather than as nix.conf's"
    assert seen["written"] == {"max-jobs": "9", "keep-going": "true"}
    assert seen["read_back"] == "9", "the write must reach the process that holds the globals"
    assert seen["read_sizes"][0] < seen["read_sizes"][1], "overridden_only must filter"


@pytest.mark.parametrize("engine", ["inproc", "rpc"])
async def test_writing_settings_is_refused_while_a_store_or_evaluator_is_open(
    shared_nix_environment: NixTestEnvironment,
    engine: str,
) -> None:
    """The guard, and the reason for it.

    Nix builds a store and an evaluator from the globals as they stand, and
    neither looks again. Refusing the write is what makes ``set_settings``
    honest without a hand-maintained list of settings that are safe to change
    late -- and that list would be the fragile part, because one wrong entry
    reinstates exactly the silent drop this module exists to remove.
    """
    seen: dict[str, Any] = {}
    async with _open_session(shared_nix_environment, engine) as session:
        # Nothing is open yet, so the write lands.
        seen["before"] = await session.set_settings(nanopynix.NixGlobalSettings(max_jobs=6))

        async with session.store() as store:
            with pytest.raises(SettingNotLiveError, match="1 store is open") as store_error:
                await session.set_settings(nanopynix.NixGlobalSettings(max_jobs=7))
            seen["store_message"] = str(store_error.value)

            async with session.eval(store) as evaluator:
                await evaluator.string("1")
                with pytest.raises(SettingNotLiveError, match="evaluator") as eval_error:
                    await session.set_settings(nanopynix.NixGlobalSettings(max_jobs=7))
                seen["eval_message"] = str(eval_error.value)

        # And once both are closed again, the same write is fine.
        seen["after"] = await session.set_settings(nanopynix.NixGlobalSettings(max_jobs=7))

    note(**{f"{engine}/guard": seen})
    assert seen["before"] == {"max-jobs": "6"}
    assert seen["after"] == {"max-jobs": "7"}
    for message in (seen["store_message"], seen["eval_message"]):
        assert "Close them first" in message, "the message must say what to do about it"


@pytest.mark.parametrize("engine", ["inproc", "rpc"])
async def test_writing_settings_leaves_the_unnamed_ones_alone(
    shared_nix_environment: NixTestEnvironment,
    engine: str,
) -> None:
    """A write carries the fields the caller named, and no other.

    ``experimental_features`` has a default, so rendering every non-``None``
    field would make ``set_settings(NixGlobalSettings(max_jobs=5))`` also reset
    the feature list -- silently dropping whatever a namespaced session enabled
    beyond the defaults, ``local-overlay-store`` among them. Measured before
    the fix, that one call wrote ``experimental-features`` back to the six
    defaults. It is the same class of silent loss as the rest of this module,
    arriving through the model instead of through Nix.
    """
    async with _open_session(shared_nix_environment, engine) as session:
        before = (await session.settings())["experimental-features"]
        written = await session.set_settings(nanopynix.NixGlobalSettings(max_jobs=5))
        after = (await session.settings())["experimental-features"]

    note(experimental_features_before=before, experimental_features_after=after, written=written)
    assert written == {"max-jobs": "5"}, "only the named setting may be written"
    assert after == before, "an unnamed setting must survive a write"
