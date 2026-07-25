from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pytest_agent._history import (
    MAX_LABEL_CHARS,
    RUN_LOCK_NAME,
    RUN_LOCK_STALE_AFTER,
    RUN_META_NAME,
    append_run_record,
    create_run_lock,
    next_run_dir,
    prune_old_runs,
    read_run_meta,
    release_run_lock,
    run_label,
    run_number_of,
    validate_run_label,
    write_run_meta,
)

if TYPE_CHECKING:
    from collections.abc import Callable


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
    # The labeled budget is clamped by the same floor, so a misconfigured
    # keep=0 still leaves a labeled run its place rather than none.
    write_run_meta(tmp_path / "runs-0001", {"label": "kept"})

    prune_old_runs(tmp_path, keep=0, protect=tmp_path / "runs-0003")

    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == ["runs-0001", "runs-0003"]


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


def test_a_run_directory_knows_its_own_number() -> None:
    assert run_number_of(Path("/anywhere/runs-0007")) == 7
    # Not every directory under an agent dir is a run: history.jsonl's
    # neighbours, an editor's scratch dir, anything a user drops in there.
    assert run_number_of(Path("/anywhere/history.jsonl")) is None


def test_a_labels_meta_survives_a_write_read_round_trip(tmp_path: Path) -> None:
    write_run_meta(tmp_path, {"run": 3, "label": "nightly", "pid": 42})

    assert run_label(tmp_path) == "nightly"
    meta = read_run_meta(tmp_path)
    assert meta is not None
    assert meta["run"] == 3
    # No leftover temp file: the atomic write renames rather than copies, and
    # a stray meta.json.tmp in every run directory would be litter an agent
    # listing the directory has to explain away.
    assert sorted(p.name for p in tmp_path.iterdir()) == [RUN_META_NAME]


def test_an_unlabeled_run_has_no_label(tmp_path: Path) -> None:
    write_run_meta(tmp_path, {"run": 3, "label": None})

    assert run_label(tmp_path) is None


def test_reading_meta_that_is_missing_or_damaged_is_not_an_error(tmp_path: Path) -> None:
    # Read while pruning, on every run's sessionfinish, across directories
    # owned by other processes -- including one killed between claiming its
    # directory and writing its meta. A raise here would turn one run's bad
    # luck into every later run failing at sessionfinish.
    assert read_run_meta(tmp_path) is None
    assert run_label(tmp_path) is None

    (tmp_path / RUN_META_NAME).write_text('{"label": "half-writ', encoding="utf-8")
    assert read_run_meta(tmp_path) is None
    assert run_label(tmp_path) is None

    # Valid JSON, wrong shape -- someone's script, or a future format.
    (tmp_path / RUN_META_NAME).write_text("[1, 2, 3]", encoding="utf-8")
    assert read_run_meta(tmp_path) is None
    assert run_label(tmp_path) is None


def test_prune_old_runs_gives_labeled_runs_a_budget_of_their_own(tmp_path: Path) -> None:
    # The case labels exist for: a long suite started in the background falls
    # out of the newest N while the focused runs done alongside it churn
    # through the rotation. Pruning it would mean the agent that labeled it
    # comes back to nothing -- which is worse than not having labeled it,
    # because it planned around being able to ask.
    for n in range(1, 6):
        (tmp_path / f"runs-{n:04d}").mkdir()
    write_run_meta(tmp_path / "runs-0001", {"label": "full-suite"})

    prune_old_runs(tmp_path, keep=2, protect=tmp_path / "runs-0005")

    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == ["runs-0001", "runs-0004", "runs-0005"]


def test_the_labeled_budget_is_bounded_like_any_other(tmp_path: Path) -> None:
    # A label buys a run a place in a second rotation, not immortality: an
    # agent labeling every run must not turn --agent-keep-runs into a no-op
    # and grow the archive without limit.
    for n in range(1, 6):
        (tmp_path / f"runs-{n:04d}").mkdir()
        write_run_meta(tmp_path / f"runs-{n:04d}", {"label": f"run-{n}"})

    prune_old_runs(tmp_path, keep=2, protect=tmp_path / "runs-0005")

    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == ["runs-0004", "runs-0005"]


def test_pruning_survives_a_run_whose_meta_is_damaged(tmp_path: Path) -> None:
    # Pruning reads every run's meta on every sessionfinish. One unreadable
    # file must cost that run its retention, not every later run its ability
    # to finish.
    for n in range(1, 5):
        (tmp_path / f"runs-{n:04d}").mkdir()
    (tmp_path / "runs-0001" / RUN_META_NAME).write_text("{not json", encoding="utf-8")

    prune_old_runs(tmp_path, keep=1, protect=tmp_path / "runs-0004")

    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == ["runs-0004"]


def test_a_usable_run_label_is_accepted_unchanged() -> None:
    for label in ("nightly", "full-suite", "run_2", "v1.2.3", "A"):
        assert validate_run_label(label) == label


def test_an_all_digit_run_label_is_refused() -> None:
    # `--run` reads a number as a run number, so a run labeled "123" would
    # make `--run 123` mean two different runs depending on what else is on
    # disk. Refusing at the writing end means the reading end never guesses.
    with pytest.raises(ValueError, match="all digits"):
        validate_run_label("123")


def test_a_run_label_that_would_need_quoting_is_refused() -> None:
    for label in ("", "with space", "slash/es", "-leading-dash", "quote'd", "x" * (MAX_LABEL_CHARS + 1)):
        with pytest.raises(ValueError):  # noqa: PT011 -- each case has its own message; the shared claim is that all are refused
            validate_run_label(label)
