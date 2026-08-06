from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from pynix import Pynix

if TYPE_CHECKING:
    from nanopynix.models import StorePath
    from tests.support.nix_environment import NixTestEnvironment


async def test_path_info(
    shared_nix_environment: NixTestEnvironment,
    seeded_store_path: StorePath,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = str(seeded_store_path)

    cmd = Pynix.parse(["path-info", path, *shared_nix_environment.pynix_store_args()])
    await cmd.astart()
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["path"] == path
    assert isinstance(result["narHash"], str)
    assert isinstance(result["narSize"], int)
    assert result["narSize"] > 0
    assert isinstance(result["references"], list)


async def test_path_info_nonexistent(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The failure goes to stderr, and stdout stays empty.

    The output of this command is JSON, so a caller writes
    ``pynix path-info ... | jq``. This test asserted ``captured.out`` until
    the error moved, and the message it asserted was the one that reached
    ``jq`` instead of the JSON. ``_util.error_console`` carries the rule.
    """
    cmd = Pynix.parse(
        ["path-info", "/nix/store/deadbeef-nonexistent", *shared_nix_environment.pynix_store_args()],
    )
    with pytest.raises(SystemExit):
        await cmd.astart()
    captured = capsys.readouterr()

    assert "Error" in captured.err
    assert captured.out == ""
    # The message of Nix arrives whole. `Text.from_ansi` turns the colour of
    # Nix into a style of rich, which keeps it for a terminal and drops it for
    # a pipe. Interpolating the same text into a markup string instead loses
    # the words: rich reads the `[35;1m` of Nix as a tag and eats it, so this
    # asserts the words that such a tag sits next to.
    assert "deadbeef-nonexistent" in captured.err
    assert "error:" in captured.err
