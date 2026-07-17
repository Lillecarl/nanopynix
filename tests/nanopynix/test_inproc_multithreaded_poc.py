"""Focused stress tests for native multithreaded L2 execution."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid

import pytest

import nanopynix_util
from nanopynix import inproc
from nanopynix.settings import NixSettings


_TEST_SETTINGS = NixSettings(max_jobs=25)


def _session(*, store_workers: int = 4) -> inproc.Session:
    return inproc.Session(load_config=False, settings=_TEST_SETTINGS, store_workers=store_workers)


def _wait_for_peer(barrier: threading.Barrier) -> int:
    barrier.wait(timeout=5)
    return threading.get_ident()


def _emit_log(message: str) -> int:
    nanopynix_util._log_test(message)
    return threading.get_ident()


def _block_until_released(started: threading.Event, release: threading.Event) -> None:
    started.set()
    if not release.wait(timeout=5):
        raise TimeoutError("test did not release Store worker")


async def _sleep_derivations(
    evaluator: inproc.EvalSession,
    *,
    count: int,
    seconds: int,
) -> list[inproc.Value]:
    function = await evaluator.string(_SLEEP_DERIVATION)
    seconds_function = await function.call(seconds)
    return [
        await seconds_function.call(f"{index}-{uuid.uuid4().hex}")
        for index in range(count)
    ]


async def _release_all(values: list[inproc.Value]) -> None:
    await asyncio.gather(*(value.release() for value in values))


async def _derived_paths(values: list[inproc.Value]) -> list[str]:
    return list(await asyncio.gather(*(value.get_derived_path() for value in values)))


_SLEEP_DERIVATION = """
let pkgs = import <nixpkgs> {}; in
seconds: nonce:
  pkgs.stdenvNoCC.mkDerivation {
    pname = "nanopynix-concurrent-build";
    version = nonce;
    dontUnpack = true;
    buildPhase = '' sleep ${toString seconds} '';
    installPhase = '' echo built > "$out" '';
  }
"""


@pytest.mark.anyio
async def test_inproc_store_pool_runs_work_on_two_threads() -> None:
    async with _session(store_workers=2) as nix, nix.store() as store:
        barrier = threading.Barrier(2)
        first, second = await asyncio.gather(
            nix.run(_wait_for_peer, barrier),
            nix.run(_wait_for_peer, barrier),
        )
        assert first != second

        uri, store_dir = await asyncio.gather(store.uri(), store.store_dir())
        assert uri
        assert store_dir.startswith("/")


@pytest.mark.anyio
async def test_inproc_evaluators_are_parallel_and_share_a_store() -> None:
    async with _session(store_workers=2) as nix, nix.store() as store:
        first_eval = nix.eval(store)
        second_eval = nix.eval(store)
        await asyncio.gather(first_eval.open(), second_eval.open())

        barrier = threading.Barrier(2)
        first_thread, second_thread = await asyncio.gather(
            first_eval.run(_wait_for_peer, barrier),
            second_eval.run(_wait_for_peer, barrier),
        )
        assert first_thread != second_thread

        first, second = await asyncio.gather(
            first_eval.string("21 * 2"),
            second_eval.string("6 * 7"),
        )
        assert await asyncio.gather(first.as_int(), second.as_int()) == [42, 42]
        await asyncio.gather(first_eval.close(), second_eval.close())


@pytest.mark.anyio
async def test_inproc_parallel_evaluation_stress() -> None:
    async with _session(store_workers=4) as nix, nix.store() as store:
        evaluators = [nix.eval(store) for _ in range(4)]
        await asyncio.gather(*(evaluator.open() for evaluator in evaluators))
        for _ in range(20):
            values = await asyncio.gather(
                *(evaluator.string("builtins.foldl' (a: b: a + b) 0 (builtins.genList (x: x) 1000)") for evaluator in evaluators)
            )
            assert await asyncio.gather(*(value.as_int() for value in values)) == [499500] * 4
        await asyncio.gather(*(evaluator.close() for evaluator in evaluators))


@pytest.mark.anyio
async def test_inproc_parallel_values_navigate_force_and_release() -> None:
    """Independent evaluators may exercise their complete Value lifecycle concurrently."""
    async with _session(store_workers=4) as nix, nix.store() as store:
        evaluators = [nix.eval(store) for _ in range(4)]
        await asyncio.gather(*(evaluator.open() for evaluator in evaluators))
        try:
            for index in range(30):
                values = await asyncio.gather(
                    *(evaluator.string(f"{{ nested = {{ value = {index}; }}; list = [ {index} ]; }}") for evaluator in evaluators)
                )
                nested = await asyncio.gather(*(value.attr("nested") for value in values))
                children = await asyncio.gather(*(value.attr("value") for value in nested))
                list_values = await asyncio.gather(*(value.attr("list") for value in values))
                elements = await asyncio.gather(*(value.list_get(0) for value in list_values))
                assert await asyncio.gather(*(value.as_int() for value in children)) == [index] * 4
                assert await asyncio.gather(*(value.as_int() for value in elements)) == [index] * 4
                await asyncio.gather(
                    *(value.release() for value in (*values, *nested, *children, *list_values, *elements))
                )
        finally:
            await asyncio.gather(*(evaluator.close() for evaluator in evaluators))


@pytest.mark.anyio
async def test_inproc_evaluator_keeps_one_thread_for_its_entire_lifetime() -> None:
    async with _session(store_workers=2) as nix, nix.store() as store:
        evaluator = nix.eval(store)
        await evaluator.open()
        try:
            first_thread = await evaluator.run(threading.get_ident)
            value = await evaluator.string("{ answer = 6 * 7; }")
            answer = await value.attr("answer")
            assert await answer.as_int() == 42
            assert await evaluator.run(threading.get_ident) == first_thread
            await asyncio.gather(value.release(), answer.release())
        finally:
            await evaluator.close()


@pytest.mark.anyio
async def test_inproc_store_metadata_and_closure_queries_run_concurrently() -> None:
    """One shared Store supports concurrent cache-miss metadata work."""
    async with _session(store_workers=4) as nix, nix.store() as store:
        paths = await store.query_all_valid_paths()
        if not paths:
            pytest.skip("test store contains no valid paths")
        selected = paths[: min(4, len(paths))]
        infos, closures = await asyncio.gather(
            asyncio.gather(*(store.query_path_info(path) for path in selected)),
            asyncio.gather(*(store.compute_fs_closure(path) for path in selected)),
        )
        assert [info.path for info in infos] == selected
        assert all(path in closure for path, closure in zip(selected, closures, strict=True))


@pytest.mark.anyio
async def test_inproc_logs_keep_operation_ids_isolated_between_store_threads() -> None:
    """Logger request context is thread-local and restored after each job."""
    async with _session(store_workers=2) as nix:
        events: asyncio.Queue[object] = asyncio.Queue()
        subscription = nix.subscribe(events.put_nowait)
        try:
            first_thread, second_thread = await asyncio.gather(
                nix.run(_emit_log, "first concurrent operation"),
                nix.run(_emit_log, "second concurrent operation"),
            )
            assert first_thread != second_thread

            messages: dict[str, int] = {}
            while len(messages) != 2:
                event = await asyncio.wait_for(events.get(), timeout=5)
                if event.action != "msg":
                    continue
                message = event.args[-1]
                if message in {"first concurrent operation", "second concurrent operation"}:
                    messages[message] = event.request_id
            assert all(request_id > 0 for request_id in messages.values())
            assert len(set(messages.values())) == 2
        finally:
            subscription.unsubscribe()


@pytest.mark.anyio
async def test_inproc_close_wait_false_preserves_running_store_work() -> None:
    session = _session(store_workers=1)
    await session.open()
    started = threading.Event()
    release = threading.Event()
    work = asyncio.create_task(session.run(_block_until_released, started, release))
    while not started.is_set():
        await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="outstanding"):
        await session.close(wait=False)
    release.set()
    await work
    await session.close()


@pytest.mark.anyio
async def test_inproc_forced_store_close_invalidates_dependent_evaluator() -> None:
    async with _session(store_workers=2) as nix:
        store = nix.store()
        await store.open()
        evaluator = nix.eval(store)
        await evaluator.open()
        value = await evaluator.string("42")
        await store.close(force=True)
        with pytest.raises(inproc.InprocSessionClosedError, match="EvalSession is not open"):
            await value.as_int()


@pytest.mark.anyio
async def test_inproc_independent_builds_overlap_without_cpu_pressure() -> None:
    """Separate Store build calls run sleeping builders concurrently."""
    async with _session(store_workers=2) as nix, nix.store() as store:
        first_eval = nix.eval(store)
        second_eval = nix.eval(store)
        await asyncio.gather(first_eval.open(), second_eval.open())
        try:
            first_function, second_function = await asyncio.gather(
                first_eval.string(_SLEEP_DERIVATION),
                second_eval.string(_SLEEP_DERIVATION),
            )
            first_seconds, second_seconds = await asyncio.gather(
                first_function.call(2), second_function.call(2)
            )
            first, second = await asyncio.gather(
                first_seconds.call(uuid.uuid4().hex), second_seconds.call(uuid.uuid4().hex)
            )
            first_path, second_path = await asyncio.gather(first.get_derived_path(), second.get_derived_path())
            started = time.monotonic()
            first_result, second_result = await asyncio.gather(
                store.build_paths_with_results([first_path]),
                store.build_paths_with_results([second_path]),
            )
            elapsed = time.monotonic() - started
            assert first_result[0].success
            assert second_result[0].success
            assert elapsed < 3.2, f"independent two-second builds did not overlap: {elapsed:.3f}s"
        finally:
            await asyncio.gather(first_eval.close(), second_eval.close())


@pytest.mark.anyio
async def test_inproc_one_evaluator_extracts_and_builds_fifty_derivations() -> None:
    """One EvalState hands 50 DerivedPath strings to one max-jobs-limited build request."""
    async with _session(store_workers=1) as nix, nix.store() as store:
        async with nix.eval(store) as evaluator:
            values = await _sleep_derivations(evaluator, count=50, seconds=2)
            try:
                paths = await _derived_paths(values)
                started = time.monotonic()
                results = await store.build_paths_with_results(paths)
                elapsed = time.monotonic() - started
                assert len(results) == 50
                assert all(result.success for result in results)
                # max-jobs=25 permits two waves of 25 sleeping builders. Give
                # local-store startup and loaded CI hosts ample scheduling room.
                assert elapsed < 8, f"50 two-second builds exceeded two-wave budget: {elapsed:.3f}s"
            finally:
                await _release_all(values)


@pytest.mark.anyio
async def test_inproc_parallel_batch_builds_use_multiple_store_workers() -> None:
    """Several evaluator lanes can prepare independently and build concurrently."""
    async with _session(store_workers=4) as nix, nix.store() as store:
        evaluators = [nix.eval(store) for _ in range(4)]
        values: list[list[inproc.Value]] = []
        try:
            await asyncio.gather(*(evaluator.open() for evaluator in evaluators))
            values = await asyncio.gather(
                *(_sleep_derivations(evaluator, count=5, seconds=2) for evaluator in evaluators)
            )
            derived_path_groups = await asyncio.gather(*(_derived_paths(group) for group in values))
            started = time.monotonic()
            outputs = await asyncio.gather(
                *(store.build_paths_with_results(paths) for paths in derived_path_groups)
            )
            elapsed = time.monotonic() - started
            assert [len(group) for group in outputs] == [5] * 4
            assert all(result.success for group in outputs for result in group)
            assert elapsed < 5, f"four independent build requests did not overlap: {elapsed:.3f}s"
        finally:
            await asyncio.gather(*(_release_all(group) for group in values))
            await asyncio.gather(*(evaluator.close() for evaluator in evaluators))


@pytest.mark.anyio
async def test_inproc_mixed_evaluation_build_and_store_workloads() -> None:
    """Evaluation, builds, and cache-miss Store queries make progress together."""
    async with _session(store_workers=4) as nix, nix.store() as store:
        build_evaluators = [nix.eval(store) for _ in range(2)]
        query_evaluators = [nix.eval(store) for _ in range(2)]
        evaluators = [*build_evaluators, *query_evaluators]
        build_values: list[list[inproc.Value]] = []
        try:
            await asyncio.gather(*(evaluator.open() for evaluator in evaluators))
            build_values = await asyncio.gather(
                *(_sleep_derivations(evaluator, count=5, seconds=2) for evaluator in build_evaluators)
            )
            paths = await store.query_all_valid_paths()
            if not paths:
                pytest.skip("test store contains no valid paths")
            selected = paths[: min(4, len(paths))]
            build_paths = await asyncio.gather(*(_derived_paths(values) for values in build_values))
            build_tasks = [store.build_paths_with_results(paths) for paths in build_paths]
            evaluation_tasks = [
                evaluator.string("builtins.foldl' (a: b: a + b) 0 (builtins.genList (x: x) 10000)")
                for evaluator in query_evaluators
                for _ in range(5)
            ]
            query_tasks = [
                store.compute_fs_closure(path)
                for path in selected
            ]
            results = await asyncio.gather(*build_tasks, *evaluation_tasks, *query_tasks)
            built = results[: len(build_tasks)]
            evaluated = results[len(build_tasks) : len(build_tasks) + len(evaluation_tasks)]
            closures = results[len(build_tasks) + len(evaluation_tasks) :]
            assert [len(group) for group in built] == [5] * 2
            assert all(result.success for group in built for result in group)
            assert await asyncio.gather(*(value.as_int() for value in evaluated)) == [49_995_000] * len(evaluated)
            assert all(path in closure for path, closure in zip(selected, closures, strict=True))
            await _release_all(evaluated)
        finally:
            await asyncio.gather(*(_release_all(group) for group in build_values))
            await asyncio.gather(*(evaluator.close() for evaluator in evaluators))


@pytest.mark.anyio
async def test_inproc_session_close_drains_open_evaluators() -> None:
    session = _session(store_workers=2)
    await session.open()
    store = session.store()
    await store.open()
    evaluator = session.eval(store)
    await evaluator.open()
    assert await (await evaluator.string("40 + 2")).as_int() == 42

    await session.close()
