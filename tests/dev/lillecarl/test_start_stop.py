from pathlib import Path

from pynixd import Server
from pynixd.config import LocalSocketStoreSpec, PynixdSettings


async def test_start_stop(tmp_path):
    settings = PynixdSettings(config=Path("/etc/pynixd/pynixd.json"))
    settings.unix_path = None
    settings.stores["local"] = LocalSocketStoreSpec(
        store_path=tmp_path / "store",
        use_db=True,
    )

    local_store, _remote_stores = settings.to_stores()

    async with Server(local_store=local_store, settings=settings):
        pass
