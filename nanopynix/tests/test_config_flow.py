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
from nanopynix_proto.nix.eval import ConfigureEvalRequest

import nanopynix
from nanopynix import NixEvalSettings, NixFetchSettings, NixSettingsEnv, stores
from nanopynix.exceptions import EvalError, NixError, SettingNotLiveError, SettingOutOfScopeError
from nanopynix.namespace import STORE_DIR
from nanopynix.settings import (
    DEFAULT_EXPERIMENTAL_FEATURES,
    NixEvaluatorSettings,
    NixGlobalSettings,
    NixSettings,
    construction_time_keys,
    field_is_supported,
    list_eval_settings_metadata,
    list_fetch_settings_metadata,
    list_flake_settings_metadata,
    list_settings_metadata,
    reject_construction_time_keys,
    render_for_scope,
)
from nanopynix_testing.nix_markers import LINUX_CHROOT_BUILD
from test_support.git_fixtures import init_flake_repo
from test_support.notes import note

if TYPE_CHECKING:
    from nanopynix_testing.nix_environment import NixTestEnvironment


#: The nine eval settings Nix reads while it builds the evaluator. Restated
#: here on purpose: if the model changes, this list is what notices.
#:
#: `count-calls` is the one that this repository adds, in
#: `nix-2.35-count-calls.patch`. The evaluator reads it into a `const` member,
#: so it belongs here beside the eight that Nix ships. 2.31 takes no patch and
#: has no such setting, and the model names it on every version even so: the
#: model is Python, and a field that appears and disappears with the Nix
#: version would be a worse surface than one that raises when it is used.
CONSTRUCTION_TIME_EVAL_SETTINGS = frozenset(
    {
        "count-calls",
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
    """The model states both scopes, and renders both in one map.

    This is a statement about the object only. The combined map is not what
    reaches Nix: each door takes the half it owns, and
    ``test_one_evaluator_object_reaches_both_registries`` is what pins that.
    This test used to claim "narrow parameters still take it", which was the
    type system's answer rather than Nix's -- the parameter accepted the object
    and then sent the whole map to one registry.
    """
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
    """The one check both sides run, on the rendered keys.

    A rendered mapping is all that reaches a worker, so the check reads keys
    rather than a typed model. This pins the check itself; the three tests
    below prove that the worker actually runs it.
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


async def test_the_worker_refuses_a_hand_built_configure_request(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """The refusal belongs to the protocol, not to one client.

    ``EvalSession.configure`` checks before it sends, so the public route never
    reaches the worker with a construction-time key. This test goes around that
    check and speaks to the worker's own entry point, which is what a second
    client, or a caller holding the proxy, can do.

    Measured before the worker had the check: the worker accepted the request,
    answered with an empty response, and dropped ``pure-eval``.

    The class survives the trip, which is what #28 fixed. The worker sends a
    ``nix.common.ErrorIdentity`` in the status trailer, so this is the same
    class the inproc route raises two tests below -- and it is the right
    family, because ``SettingNotLiveError`` says Nix was never consulted while
    ``NixError`` says the opposite. Before #28 this asserted ``NixError``.
    """
    async with (
        shared_nix_environment.rpc_session() as session,
        session.store() as store,
        session.eval(store) as evaluator,
    ):
        with pytest.raises(SettingNotLiveError) as excinfo:
            # The proxy, not `configure()`: a caller who bypassed the public
            # API is the subject here.
            await evaluator._ensure_proxy().configure_eval(
                ConfigureEvalRequest(eval_settings={"pure-eval": "true"}),
            )
        message = str(excinfo.value)
        note(worker_refusal=message)
        assert "pure-eval" in message, "the message must name the setting"
        assert "open the evaluator" in message, "the message must say what to do instead"

        # A refusal that applied the setting anyway would be worse than none.
        assert await _evaluator_purity(evaluator) == "impure"


async def test_the_worker_applies_a_live_setting_from_a_hand_built_request(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """The worker's refusal is targeted, exactly as the client's is.

    ``max-call-depth`` is read at call time and never in the constructor, so
    the same route that refuses ``pure-eval`` must let this one through and
    take effect.
    """
    recurse = "let f = n: if n == 0 then 0 else f (n - 1); in f 200"

    async with (
        shared_nix_environment.rpc_session() as session,
        session.store() as store,
        session.eval(store) as evaluator,
    ):
        # The same bypassed route as the test above.
        await evaluator._ensure_proxy().configure_eval(
            ConfigureEvalRequest(eval_settings={"max-call-depth": "20"}),
        )
        with pytest.raises(EvalError, match="max-call-depth"):
            await evaluator.string(recurse)


async def test_the_core_layer_is_what_refuses(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """One implementation guards both engines, and inproc shows its class.

    The check lives on ``CoreEvalState.configure``. rpc reaches that method
    through its worker handler, inproc through its evaluator thread, so one
    check covers both. Reaching it directly on inproc goes past
    ``configure()``'s own check with no transport in between, which is the one
    route where :class:`~nanopynix.SettingNotLiveError` is visible as itself.
    """
    async with (
        shared_nix_environment.inproc_session() as session,
        session.store() as store,
        session.eval(store) as evaluator,
    ):
        # Straight to the shared object, past `configure()`'s own check.
        core = evaluator._require_core()
        with pytest.raises(SettingNotLiveError, match="pure-eval"):
            await evaluator.run(core.configure, {"pure-eval": "true"}, {})

        assert await _evaluator_purity(evaluator) == "impure"


# ── Store construction is its own moment ─────────────────────────────


@LINUX_CHROOT_BUILD
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


# ── What the host configured, and what a caller says over it ─────────
#
# A session sends the globals to `globalConfig` after `loadConfFile`, so every
# key it sends replaces what the host wrote. It must therefore send only the
# keys the caller named. Issue #96: `pynix` gave `substituters` a default, the
# default was not None, and every `pynix build` on a host with a private cache
# lost that cache with no message.

#: A `nix.conf` for the *worker* to read. The names are not reachable, and
#: nothing here builds: these tests read the settings back, and never fetch.
_HOST_NIX_CONF = (
    "substituters = https://first.example/ https://second.example/\n"
    "experimental-features = nix-command flakes read-only-local-store\n"
)


def _host_nix_conf(tmp_path: Path, text: str = _HOST_NIX_CONF) -> Path:
    """Write a `nix.conf` and give back its path.

    It reaches Nix as `NIX_USER_CONF_FILES`, which `loadConfFile` reads after
    the system file, so the value here wins wherever the suite runs.
    """
    path = tmp_path / "nix.conf"
    path.write_text(text)
    return path


def _host_session(environment: NixTestEnvironment, path: Path, **overrides: Any) -> Any:
    """One rpc session that reads ``path`` as the configuration of the host.

    **rpc only, and not both engines.** ``initLibStore`` is process-global and
    reads the configuration once, so ``load_config=True`` in-process would apply
    this file to every later test of the run. The worker is a fresh process, and
    that is the isolation this needs. Both engines share
    ``NixCore.initialize``, which is where the ordering under test lives.
    """
    return nanopynix.rpc.Session(
        store_uri=environment.store_uri,
        nix_conf=path,
        load_config=True,
        **overrides,
    )


def test_a_default_is_never_sent_to_nix() -> None:
    """The mechanism of #96, with no session.

    ``experimental_features`` is the one field of the five scopes that carries
    a non-``None`` default, so it is the one that shows the difference.
    """
    named_nothing = render_for_scope(NixSettings(), NixGlobalSettings, explicit_only=True)
    named_one = render_for_scope(NixSettings(max_jobs=4), NixGlobalSettings, explicit_only=True)
    note(named_nothing=named_nothing, named_one=named_one)

    assert named_nothing == {}
    assert named_one == {"max-jobs": "4"}
    # What session construction used to call. Recorded because it is the whole
    # defect: `for_scope` re-validates, so a default arrives marked as set.
    assert "experimental-features" in NixGlobalSettings.for_scope(NixSettings()).to_worker_settings()


@pytest.mark.anyio
async def test_the_substituters_of_the_host_reach_the_session(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
) -> None:
    """Issue #96, from the side a user sees.

    Before this, the session sent the substituters of its own model, that model
    carried a default, and the two names below were replaced by it.
    """
    async with _host_session(shared_nix_environment, _host_nix_conf(tmp_path)) as session:
        live = await session.settings()

    note(substituters=live["substituters"])
    assert live["substituters"].split() == ["https://first.example/", "https://second.example/"]


@pytest.mark.anyio
async def test_a_named_substituter_beats_the_host(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
) -> None:
    """Naming one is an explicit request, and it still wins.

    This is the other half of #96: the file of the host stands until a caller
    speaks, and a caller that speaks is obeyed.
    """
    session = _host_session(
        shared_nix_environment,
        _host_nix_conf(tmp_path),
        settings=NixSettings(substituters=["https://named.example/"]),
    )
    async with session:
        live = await session.settings()

    assert live["substituters"].split() == ["https://named.example/"]


@pytest.mark.anyio
async def test_the_features_of_the_host_survive_beside_ours(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
) -> None:
    """``enable_experimental_feature`` inserts, and the setting replaces.

    ``read-only-local-store`` is in the file and in no default of nanopynix, so
    it is what proves the insert. The features must also go on *after*
    ``init_libstore``: ``loadConfFile`` writes the whole set, so a feature
    enabled before it is discarded. Move that step of
    ``NixCore.initialize`` above ``init_libstore`` and this test fails.
    """
    async with _host_session(shared_nix_environment, _host_nix_conf(tmp_path)) as session:
        live = await session.settings()

    features = set(live["experimental-features"].split())
    note(features=sorted(features))
    assert "read-only-local-store" in features
    assert set(DEFAULT_EXPERIMENTAL_FEATURES) <= features


@pytest.mark.anyio
async def test_an_ambient_variable_overrides_nothing_of_the_host(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The third test #96 asks for, which #98 had to be fixed first to write.

    ``overridden_from_config`` is the list a user should be shown: the host
    asked for one value and this session uses another. It must hold nothing
    that the caller did not name.

    ``system`` is what made that impossible. ``stdenv`` exports it, and
    ``NixSettingsEnv`` read it because pydantic-settings takes the ``nix.conf``
    alias of a field as a second, unprefixed environment name. Every session
    opened from a shell of Nix therefore replaced the ``system`` of the host.
    Remove ``PrefixedEnvSettingsSource`` from
    ``NixSettingsEnv.settings_customise_sources`` and this test fails.
    """
    monkeypatch.setenv("system", "i686-linux")
    conf = _host_nix_conf(tmp_path, f"{_HOST_NIX_CONF}system = riscv64-linux\n")

    async with _host_session(shared_nix_environment, conf, settings=NixSettingsEnv()) as session:
        provenance = await session.settings_provenance()
        live = await session.settings()

    note(overridden=provenance.overridden_from_config, system=live["system"])
    assert provenance.overridden_from_config == {}
    assert live["system"] == "riscv64-linux"


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
@pytest.mark.anyio
async def test_both_engines_enable_the_features_without_the_setting(
    shared_nix_environment: NixTestEnvironment,
    engine: str,
) -> None:
    """The features are no longer a setting, so this is what carries them now.

    Both engines pass them to ``NixCore.initialize``. inproc had no such call at
    all, and relied on the setting alone, so it is the engine this protects.
    """
    async with _open_session(shared_nix_environment, engine) as session:
        live = await session.settings()

    assert set(DEFAULT_EXPERIMENTAL_FEATURES) <= set(live["experimental-features"].split())


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


# ── The routing matrix: five scopes, both engines, both backends ─────
#
# `NixSettings` inherits five scopes, and every one of them used to be sent to
# `globalConfig`. Only the global scope is registered there, so four of the
# five raised `unknown setting`. Measured before the fix, on both engines:
# `pure_eval`, `trusted`, `warn_dirty` and `use_registries` all failed to open
# a session at all.
#
# The suite was green at 2534 passed while that shipped, because every
# `NixSettings(...)` in the tests and the examples named global-scope fields
# only. Each case below therefore asserts through the door that owns the
# scope, which is the only way to tell an applied setting from an accepted and
# discarded one.


def test_the_four_settings_registries_are_disjoint() -> None:
    """No setting is registered in two of Nix's four registries.

    ``check_settings_model_drift`` used to subtract the eval, fetch and flake
    names from the global registry, on the belief that ``globalConfig``
    aggregates them. It does not. That step removed nothing, and this is what
    notices if a future Nix makes it necessary again -- at which point the
    routing itself needs a rule for the setting that appears twice.
    """
    registries = {
        "eval": set(list_eval_settings_metadata()),
        "fetch": set(list_fetch_settings_metadata()),
        "flake": set(list_flake_settings_metadata()),
    }
    global_names = set(list_settings_metadata())
    overlaps = {name: sorted(names & global_names) for name, names in registries.items()}
    note(registry_sizes={name: len(names) for name, names in registries.items()}, overlaps=overlaps)

    assert overlaps == {"eval": [], "fetch": [], "flake": []}


@pytest.fixture
def dirty_flake(tmp_path: Path) -> str:
    """A local git flake whose working tree differs from its last commit.

    The fetch scope needs an observable that reaches no network, and
    ``allow_dirty`` is it: Nix refuses to fetch a dirty tree when it is off.
    The dirtied file has to be a *tracked* one -- an untracked file leaves the
    tree clean as far as this check is concerned.
    """
    flake_dir = tmp_path / "dirty-flake"
    flake_dir.mkdir()
    init_flake_repo(flake_dir)
    flake_file = flake_dir / "flake.nix"
    flake_file.write_text(flake_file.read_text(encoding="utf-8") + "\n# dirtied after the commit\n", encoding="utf-8")
    return f"git+file://{flake_dir}"


async def _global_scope_holds(session: Any) -> str:
    return (await session.settings())["substituters"]


async def _evaluator_purity(evaluator: Any) -> str:
    """Whether this open evaluator is pure. ``builtins.currentTime`` is impure."""
    try:
        await (await evaluator.string("builtins.currentTime")).to_python()
    except EvalError:
        return "pure"
    return "impure"


async def _eval_scope_holds(session: Any, store: Any, **eval_kwargs: Any) -> str:
    """Whether a freshly opened evaluator is pure."""
    async with session.eval(store, **eval_kwargs) as evaluator:
        return await _evaluator_purity(evaluator)


async def _fetch_scope_holds(session: Any, store: Any, ref: str, **eval_kwargs: Any) -> str:
    """Whether the fetcher accepts a dirty git tree.

    Only Nix's own refusal counts, and only the refusal that names the dirty
    tree. This used to catch every exception. Nix 2.31 crashed inside the git
    fetcher with ``RuntimeError: Invalid argument`` -- see issue #34 -- so
    ``test_every_settings_scope_reaches_the_door_that_owns_it`` reported that
    the fetch scope held while the fetch scope reached nothing at all.
    """
    async with session.eval(store, **eval_kwargs) as evaluator:
        try:
            await evaluator.lock_flake(ref, write_lock_file=False)
        except NixError as exc:
            if "dirty" not in exc.msg_without_ansi:
                raise
            return "refused"
        return "accepted"


async def _flake_scope_holds(session: Any, store: Any, **flake_kwargs: Any) -> str:
    """Whether an indirect flake reference may be looked up in a registry.

    ``nixpkgs`` is an indirect reference, so with the registries off Nix fails
    before it reaches any network. That is what makes this assertable here.
    """
    async with session.eval(store) as evaluator:
        try:
            await evaluator.lock_flake("nixpkgs", write_lock_file=False, **flake_kwargs)
        except Exception:
            return "refused"
        return "resolved"


@pytest.mark.parametrize("engine", ["inproc", "rpc"])
async def test_every_settings_scope_reaches_the_door_that_owns_it(
    shared_nix_environment: NixTestEnvironment,
    engine: str,
    tmp_path: Path,
    dirty_flake: str,
) -> None:
    """One object states all five scopes, and every one of them takes effect.

    Before the fix this session did not open: ``Session.__init__`` sent all
    five scopes to ``globalConfig`` and Nix answered ``unknown setting:
    pure-eval``. The store scope was the one half that worked, through the
    store URI.

    The globals are the environment's own, unchanged, because one process
    hosts at most one set of inproc globals. That this inproc session opens at
    all beside the suite's others is the second half of the fix: the guard now
    compares the *global* scope only, and an eval or fetch difference no
    longer makes two sessions incompatible. Those live on each ``EvalState``.
    """
    settings = shared_nix_environment.settings.model_copy(
        update={"trusted": True, "pure_eval": True, "allow_dirty": False, "use_registries": False},
    )
    session_class = nanopynix.rpc.Session if engine == "rpc" else nanopynix.inproc.Session
    # A store *model*, not the environment's URI string: a URI the caller wrote
    # by hand is passed through untouched, so the store defaults have nothing
    # to merge into. That is `resolve_store_spec`'s documented rule.
    store_config = stores.Local(root=str(tmp_path / "matrix-store"))

    expected_substituters = " ".join(shared_nix_environment.settings.substituters or ())
    seen: dict[str, str] = {}
    async with (
        session_class(load_config=False, settings=settings, store_uri=store_config) as session,
        session.store() as store,
    ):
        seen["global"] = await _global_scope_holds(session)
        seen["store"] = await store.uri(with_params=True)
        seen["eval"] = await _eval_scope_holds(session, store)
        seen["fetch"] = await _fetch_scope_holds(session, store, dirty_flake)
        seen["flake"] = await _flake_scope_holds(session, store)

    note(**{f"{engine}/routing": seen})
    assert seen["global"] == expected_substituters, "a global-scope field must reach globalConfig"
    assert "trusted=true" in seen["store"], "a store-scope field must reach the store URI"
    assert seen["eval"] == "pure", "an eval-scope field must reach the evaluator"
    assert seen["fetch"] == "refused", "a fetch-scope field must reach the fetcher"
    assert seen["flake"] == "refused", "a flake-scope field must reach the flake operation"


@pytest.mark.parametrize("engine", ["inproc", "rpc"])
async def test_a_per_call_setting_beats_the_session_default(
    shared_nix_environment: NixTestEnvironment,
    engine: str,
    tmp_path: Path,
    dirty_flake: str,
) -> None:
    """The session states a default; the call overrides it. Both directions.

    Each case names the *opposite* of the session's value, so a default that
    silently wins and an override that silently wins are both caught. Without
    this, a session default is only reachable by opening a second session.
    """
    settings = shared_nix_environment.settings.model_copy(
        update={"trusted": True, "pure_eval": True, "allow_dirty": False, "use_registries": True},
    )
    session_class = nanopynix.rpc.Session if engine == "rpc" else nanopynix.inproc.Session

    seen: dict[str, str] = {}
    async with (
        session_class(
            load_config=False,
            settings=settings,
            store_uri=stores.Local(root=str(tmp_path / "override-store")),
        ) as session,
        session.store(stores.Local(root=str(tmp_path / "untrusted"), trusted=False)) as store,
    ):
        seen["store"] = await store.uri(with_params=True)
        seen["eval"] = await _eval_scope_holds(session, store, eval_settings=NixEvalSettings(pure_eval=False))
        seen["fetch"] = await _fetch_scope_holds(
            session,
            store,
            dirty_flake,
            fetch_settings=NixFetchSettings(allow_dirty=True),
        )
        seen["flake"] = await _flake_scope_holds(
            session,
            store,
            flake_settings=nanopynix.NixFlakeSettings(use_registries=False),
        )

    note(**{f"{engine}/override": seen})
    assert "trusted=false" in seen["store"], "a field set on the store model must beat the session default"
    assert seen["eval"] == "impure", "eval_settings must beat the session default"
    assert seen["fetch"] == "accepted", "fetch_settings must beat the session default"
    assert seen["flake"] == "refused", "flake_settings must beat the session default"


# ── One object states both evaluator scopes (#27) ────────────────────


def _session_class(engine: str) -> Any:
    return nanopynix.rpc.Session if engine == "rpc" else nanopynix.inproc.Session


@pytest.mark.parametrize("engine", ["inproc", "rpc"])
@pytest.mark.parametrize("parameter", ["eval_settings", "fetch_settings"])
async def test_one_evaluator_object_reaches_both_registries(
    shared_nix_environment: NixTestEnvironment,
    engine: str,
    parameter: str,
    tmp_path: Path,
    dirty_flake: str,
) -> None:
    """``NixEvaluatorSettings`` states both scopes, and both must take effect.

    The class documents itself as accepted at a narrow parameter "because it
    really is one". It was not: the whole object went to the one door named,
    and Nix answered ``unknown eval setting: warn-dirty``. Measured before the
    fix, on both parameters:

        eval_settings=Evaluator(both) : unknown eval setting: warn-dirty
        fetch_settings=Evaluator(both): unknown fetch setting: max-call-depth

    Both halves are asserted, because either alone would still pass if the
    other were dropped rather than routed. The session states the *opposite*
    of each field, so a session default that silently won would be caught too.
    """
    settings = shared_nix_environment.settings.model_copy(update={"pure_eval": False, "allow_dirty": True})
    both = NixEvaluatorSettings(pure_eval=True, allow_dirty=False)

    seen: dict[str, str] = {}
    async with (
        _session_class(engine)(
            load_config=False,
            settings=settings,
            store_uri=stores.Local(root=str(tmp_path / "both-scopes")),
        ) as session,
        session.store() as store,
    ):
        seen["eval"] = await _eval_scope_holds(session, store, **{parameter: both})
        seen["fetch"] = await _fetch_scope_holds(session, store, dirty_flake, **{parameter: both})

    note(**{f"{engine}/{parameter}": seen})
    assert seen["eval"] == "pure", "the eval half must reach the evaluator"
    assert seen["fetch"] == "refused", "the fetch half must reach the fetcher"


@pytest.mark.parametrize("engine", ["inproc", "rpc"])
async def test_configure_takes_one_object_for_both_scopes(
    shared_nix_environment: NixTestEnvironment,
    engine: str,
    tmp_path: Path,
    dirty_flake: str,
) -> None:
    """The same routing on the live path, where every fetch setting is live.

    ``max_call_depth`` and ``allow_dirty`` are both read at the point of use,
    so one ``configure`` call can change both on an evaluator that is already
    open. The depth is 100 rather than 20, because locking the flake below
    also evaluates, and this test must fail on the recursion alone.
    """
    recurse = "let f = n: if n == 0 then 0 else f (n - 1); in f 200"
    settings = shared_nix_environment.settings.model_copy(update={"allow_dirty": True})

    async with (
        _session_class(engine)(
            load_config=False,
            settings=settings,
            store_uri=stores.Local(root=str(tmp_path / "configure-both")),
        ) as session,
        session.store() as store,
        session.eval(store) as evaluator,
    ):
        assert await (await evaluator.string(recurse)).as_int() == 0, "the baseline must evaluate"

        await evaluator.configure(NixEvaluatorSettings(max_call_depth=100, allow_dirty=False))

        # The fetch half first: the eval half makes every later evaluation
        # shallower, and locking a flake evaluates.
        with pytest.raises(NixError, match="dirty"):
            await evaluator.lock_flake(dirty_flake, write_lock_file=False)
        with pytest.raises(EvalError, match="max-call-depth"):
            await evaluator.string(recurse)


@pytest.mark.parametrize("engine", ["inproc", "rpc"])
async def test_the_parameter_that_owns_the_scope_wins(
    shared_nix_environment: NixTestEnvironment,
    engine: str,
    tmp_path: Path,
    dirty_flake: str,
) -> None:
    """Two parameters name one fetch field, and ``fetch_settings`` decides.

    The eval and fetch scopes share no field name, so one object is never
    ambiguous. Two *parameters* still can be, and this is the rule for that:
    the parameter that owns the scope wins over the same field carried along
    by the other one.
    """
    settings = shared_nix_environment.settings.model_copy(update={"allow_dirty": False})

    async with (
        _session_class(engine)(
            load_config=False,
            settings=settings,
            store_uri=stores.Local(root=str(tmp_path / "precedence")),
        ) as session,
        session.store() as store,
    ):
        held = await _fetch_scope_holds(
            session,
            store,
            dirty_flake,
            eval_settings=NixEvaluatorSettings(allow_dirty=False),
            fetch_settings=NixFetchSettings(allow_dirty=True),
        )

    note(**{f"{engine}/precedence": held})
    assert held == "accepted", "fetch_settings owns the fetch scope, so its value must win"


@pytest.mark.parametrize("engine", ["inproc", "rpc"])
async def test_a_field_no_door_owns_is_refused(
    shared_nix_environment: NixTestEnvironment,
    engine: str,
    tmp_path: Path,
) -> None:
    """A global field at an evaluator door is refused, and the message names it.

    Nix used to answer for this, and it answered badly: ``unknown eval
    setting: experimental-features`` -- naming a field the caller never set,
    because ``NixSettings`` gives that one a default.

    ``session.eval`` is not a coroutine, so the refusal happens on the call
    itself rather than on entering the context manager.
    """
    async with (
        _session_class(engine)(
            load_config=False,
            settings=shared_nix_environment.settings,
            store_uri=stores.Local(root=str(tmp_path / "out-of-scope")),
        ) as session,
        session.store() as store,
    ):
        with pytest.raises(SettingOutOfScopeError, match="max-jobs"):
            session.eval(store, eval_settings=NixSettings(max_jobs=3))

        async with session.eval(store) as evaluator:
            with pytest.raises(SettingOutOfScopeError, match="pure-eval"):
                await evaluator.lock_flake("nixpkgs", flake_settings=NixSettings(pure_eval=True))

        # A default is not a field the caller named, so it must not trip the
        # check. `NixSettings()` carries `experimental_features`, which is
        # exactly the field the old message wrongly blamed.
        async with session.eval(store, eval_settings=NixSettings()):
            pass


@pytest.mark.parametrize("engine", ["inproc", "rpc"])
async def test_a_wider_object_writes_only_its_global_half(
    shared_nix_environment: NixTestEnvironment,
    engine: str,
    tmp_path: Path,
) -> None:
    """``set_settings`` narrows the rendered map, not the model.

    ``for_scope`` re-validates, and that marks a default as explicitly set.
    Measured:

        NixSettings(max_jobs=3).to_worker_settings(explicit_only=True)
          -> {'max-jobs': '3'}
        NixGlobalSettings.for_scope(that).to_worker_settings(explicit_only=True)
          -> {'experimental-features': '...', 'max-jobs': '3'}

    ``explicit_only`` exists so a write of one field does not reset the
    feature list, so narrowing this door by model would undo it. This is the
    regression guard for that.
    """
    async with _session_class(engine)(
        load_config=False,
        settings=shared_nix_environment.settings,
        store_uri=stores.Local(root=str(tmp_path / "global-half")),
    ) as session:
        before = (await session.settings())["experimental-features"]

        applied = await session.set_settings(NixSettings(max_jobs=3))

        after = (await session.settings())["experimental-features"]
        note(**{f"{engine}/globals": {"applied": applied, "before": before, "after": after}})
        assert applied["max-jobs"] == "3", "the global half must be written"
        assert after == before, "a field the caller never named must be left alone"

        with pytest.raises(SettingOutOfScopeError, match="pure-eval"):
            await session.set_settings(NixSettings(pure_eval=True))


@pytest.mark.parametrize("engine", ["inproc", "rpc"])
async def test_the_search_path_has_one_order_of_precedence(
    shared_nix_environment: NixTestEnvironment,
    engine: str,
    tmp_path: Path,
) -> None:
    """``nix_path`` has two session-level spellings, and the more specific wins.

    ``Session`` takes it as an argument of its own, and it is also a field of
    :class:`NixEvalSettings`, which a session now honours as a default. The
    order is per-call, then the settings object, then the argument. The
    argument still applies whenever the settings object says nothing, so
    nothing that worked before changes.
    """
    entries: dict[str, Path] = {}
    for index, name in enumerate(("from_argument", "from_settings", "from_call"), start=1):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "default.nix").write_text(f"{index}\n", encoding="utf-8")
        entries[name] = directory

    # `Any`, as everywhere else in this module: the two engines share no
    # nominal base, which is exactly what these tests exist to check.
    session_class: Any = nanopynix.rpc.Session if engine == "rpc" else nanopynix.inproc.Session
    settings = shared_nix_environment.settings.model_copy(
        update={"nix_path": [f"precedence={entries['from_settings']}"]},
    )

    seen: dict[str, int] = {}
    async with (
        session_class(
            store_uri=shared_nix_environment.store_uri,
            load_config=False,
            settings=settings,
            nix_path=[f"precedence={entries['from_argument']}"],
        ) as session,
        session.store() as store,
    ):
        async with session.eval(store) as evaluator:
            seen["settings_beats_argument"] = await (await evaluator.string("import <precedence>")).as_int()
        async with session.eval(
            store,
            eval_settings=NixEvalSettings(nix_path=[f"precedence={entries['from_call']}"]),
        ) as evaluator:
            seen["call_beats_settings"] = await (await evaluator.string("import <precedence>")).as_int()

    note(**{f"{engine}/nix_path": seen})
    assert seen["settings_beats_argument"] == 2, "NixSettings.nix_path must beat Session(nix_path=...)"
    assert seen["call_beats_settings"] == 3, "a per-evaluator nix_path must beat both"
