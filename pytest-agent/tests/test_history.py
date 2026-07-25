from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from pytest_agent._history import (
    RUN_LOCK_NAME,
    RUN_LOCK_STALE_AFTER,
    append_run_record,
    create_run_lock,
    next_run_dir,
    prune_old_runs,
    release_run_lock,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest


def test_next_run_dir_starts_at_one(tmp_path: Path) -> None:
    n, run_dir = next_run_dir(tmp_path)
    assert n == 1
    assert run_dir == tmp_path / "runs-0001"
    assert run_dir.is_dir()


def test_next_run_dir_increments_past_existing_runs(tmp_path: Path) -> None:
    (tmp_path / "runs-0001").mkdir()
    (tmp_path / "runs-0002").mkdir()
    (tmp_path / "not-a-run-dir").mkdir()

    n, run_dir = next_run_dir(tmp_path)
    assert n == 3
    assert run_dir == tmp_path / "runs-0003"


def test_next_run_dir_retries_on_collision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate a concurrent invocation claiming runs-0001 (the number this
    # call would otherwise pick) between next_run_dir's scan and its mkdir.
    orig_mkdir: Callable[..., None] = Path.mkdir

    def flaky_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == "runs-0001":
            raise FileExistsError(self.name)
        orig_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", flaky_mkdir)

    n, run_dir = next_run_dir(tmp_path)

    assert n == 2
    assert run_dir == tmp_path / "runs-0002"


def test_append_run_record_writes_one_json_line_per_call(tmp_path: Path) -> None:
    history_path = tmp_path / "history.jsonl"
    append_run_record(history_path, {"run": 1})
    append_run_record(history_path, {"run": 2})

    lines = history_path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["run"] for line in lines] == [1, 2]


def test_a_claimed_run_directory_is_locked_until_it_is_released(tmp_path: Path) -> None:
    _n, run_dir = next_run_dir(tmp_path)
    assert (run_dir / RUN_LOCK_NAME).is_file()

    release_run_lock(run_dir)
    assert not (run_dir / RUN_LOCK_NAME).exists()
    # Releasing twice is what an interrupted run may well end up doing.
    release_run_lock(run_dir)


def test_prune_old_runs_leaves_a_run_another_session_is_still_writing(tmp_path: Path) -> None:
    # The hole `protect` cannot cover: it names only the pruning session's own
    # run, so an older concurrent run sits outside the newest N while being
    # very much alive. Deleting it destroys a session's output mid-write.
    for n in range(1, 6):
        (tmp_path / f"runs-{n:04d}").mkdir()
    create_run_lock(tmp_path / "runs-0001")

    prune_old_runs(tmp_path, keep=2, protect=tmp_path / "runs-0005")

    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == ["runs-0001", "runs-0004", "runs-0005"]


def test_prune_old_runs_reclaims_a_run_whose_session_died_holding_the_lock(tmp_path: Path) -> None:
    # A SIGKILL or a segfault never releases anything. A lock believed forever
    # would exempt that directory from pruning for good, leaking one run per
    # hard kill -- and hard kills are a case this plugin exists to serve.
    for n in range(1, 6):
        (tmp_path / f"runs-{n:04d}").mkdir()
    lock_path = create_run_lock(tmp_path / "runs-0001")
    stale = time.time() - RUN_LOCK_STALE_AFTER - 60
    os.utime(lock_path, (stale, stale))

    prune_old_runs(tmp_path, keep=2, protect=tmp_path / "runs-0005")

    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == ["runs-0004", "runs-0005"]


def test_prune_old_runs_keeps_only_the_newest_n(tmp_path: Path) -> None:
    for n in range(1, 6):
        (tmp_path / f"runs-{n:04d}").mkdir()

    prune_old_runs(tmp_path, keep=2, protect=tmp_path / "runs-0005")

    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == ["runs-0004", "runs-0005"]


def test_prune_old_runs_never_deletes_below_one_even_if_keep_is_zero_or_negative(tmp_path: Path) -> None:
    for n in range(1, 4):
        (tmp_path / f"runs-{n:04d}").mkdir()

    prune_old_runs(tmp_path, keep=0, protect=tmp_path / "runs-0003")

    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == ["runs-0003"]


def test_prune_old_runs_always_keeps_protect_even_if_it_is_not_the_newest(tmp_path: Path) -> None:
    # Simulates a concurrent run: a *later*-started, *higher*-numbered run
    # (runs-0005, from another process) can exist and even finish pruning
    # before this session's own run (runs-0003) does. This session's prune
    # call must never delete its own just-written run just because some
    # other concurrent run happens to have a higher number.
    for n in range(1, 6):
        (tmp_path / f"runs-{n:04d}").mkdir()

    prune_old_runs(tmp_path, keep=1, protect=tmp_path / "runs-0003")

    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == ["runs-0003", "runs-0005"]
