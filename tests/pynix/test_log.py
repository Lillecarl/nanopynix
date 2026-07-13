from __future__ import annotations

from nanopynix_proto.nix.store import GetBuildLogRequest

from pynix import Pynix


async def test_nanopynix_store_get_build_log_from_populated_store(populated_store: dict[str, str]):
    import nanopynix

    async with nanopynix.Session() as nix, nix.store(populated_store["store_url"]) as store:  # type: ignore[reportUnknownMemberType]  # nanopynix C++ nanobind extension without stubs
        response = await store.get_build_log(GetBuildLogRequest(path=populated_store["log_path"]))

    assert response.log is not None  # type: ignore[reportUnknownMemberType]  # response type from nanobind-backed proto
    assert "pynix-log-line" in response.log  # type: ignore[reportUnknownMemberType]  # response type from nanobind-backed proto


async def test_pynix_log_prints_build_log_from_populated_store(populated_store: dict[str, str], capsys):
    cmd = Pynix.parse(["log", populated_store["log_path"], "--store", populated_store["store_url"]])  # type: ignore[reportUnknownMemberType]  # Pynix C++ nanobind extension without full stubs
    await cmd.astart()  # type: ignore[reportUnknownMemberType]  # Pynix C++ nanobind extension without full stubs
    captured = capsys.readouterr()
    assert "pynix-log-line" in captured.out
