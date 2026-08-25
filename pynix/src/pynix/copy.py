from __future__ import annotations

from typing import override

from libpynix import opt, pos
from pynix import _impl
from pynix._nix_options import attr_option, file_option, flake_option
from pynix._settings import ConfiguredCommand, store_option


class Copy(ConfiguredCommand):
    """Copy the closure of a store path from one store to another"""

    # `nargs="*"`, so a caller who names `--file` instead gives none. The two
    # sources add up: a command that names both copies both.
    paths: list[str] = pos(help="Store paths to copy. Give none when --file or --flake names the target.")

    file: str | None = file_option()

    attr: str | None = attr_option()

    flake: str | None = flake_option()

    to: str | None = opt(None, help="Store URI to copy into. --store is the other side.")

    # `from_`, because `from` is a Python keyword. `libpynix` drops one
    # trailing underscore from the flag, so this declares `--from`.
    from_: str | None = opt(None, help="Store URI to copy out of. --store is the other side.")

    check_sigs: bool = opt(
        True,
        # `argparse.BooleanOptionalAction` writes `--check-sigs` and
        # `--no-check-sigs` from this one declaration. The second is the
        # spelling `nix copy` has, and it means the same thing here.
        negatable=True,
        help="Refuse a path that the destination store cannot verify a signature for.",
    )

    store: str = store_option("Store URI for the side that --to or --from does not name.")

    @override
    async def run(self) -> None:
        await _impl.copy.run_copy(self)
