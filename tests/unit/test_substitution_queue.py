"""Unit tests for scheduler-owned substitution queue state."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from pynixd.config import PynixdSettings
from pynixd.serde.ids import StoreId
from pynixd.store_path import StorePath
from pynixd.substitution_queue import SubstitutionHealthLog, SubstitutionQueryResult, SubstitutionQueue

if TYPE_CHECKING:
    from pynixd.context import PynixdContext


def test_new_substitution_health_log_is_healthy_until_minimum_fill() -> None:
    settings = PynixdSettings(
        substitution_health_window=10,
        substitution_health_min_fill_ratio=0.10,
    )
    health = SubstitutionHealthLog(settings)

    assert health.is_healthy

    health.record(query_succeeded=False)

    assert not health.is_healthy


def test_substitution_queue_records_positive_and_negative_results() -> None:
    ctx = cast("PynixdContext", SimpleNamespace(settings=PynixdSettings()))
    queue = SubstitutionQueue(ctx)
    path = StorePath("/nix/store/00000000000000000000000000000000-example")
    store_id = StoreId("cache")

    queue.record_query_result(
        path,
        SubstitutionQueryResult(store_id=store_id, path_info=None, query_succeeded=True),
    )

    assert path in queue.negative
    assert queue.negative[path][store_id].query_succeeded
    assert queue.should_wait_for(store_id)
