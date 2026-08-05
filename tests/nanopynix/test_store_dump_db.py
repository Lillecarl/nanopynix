"""Is the registration text we produce the text ``nix-store --dump-db`` produces?

``dump_db`` exists so that a caller can move a closure to a machine that has no
Nix, and register it there with ``nix-store --load-db``. That makes two
questions, and they are different:

* **Fidelity.** The bytes have to be the bytes Nix writes. This file asks the
  command itself, rather than restating the record format in an assertion. A
  restated format would agree with a wrong implementation as easily as with a
  right one, because the same reading of Nix produces both.
* **Effect.** The text has to build a database. The round-trip test below loads
  the registration into a store whose database has never seen the paths, and
  asks Nix whether the paths are valid there.

The second test is the one that would catch an implementation that renders a
plausible but unusable record, for example one that writes the NAR hash the way
``query_path_info`` reports it. ``query_path_info`` returns SRI, and
``makeValidityRegistration`` writes base16 with no prefix
(``store-api.cc``). Both name the same hash, and only one of them is what
``decodeValidPathInfo`` reads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio

from tests.support.subprocess_output import run_process

if TYPE_CHECKING:
    from pathlib import Path

    from nanopynix.models import StorePath
    from tests.support.nix_environment import InprocSessionFactory, NixTestEnvironment, RpcSessionFactory

# One record is five lines plus one line for each reference: the path, the NAR
# hash, the NAR size, the deriver, and the number of references.
_LINES_BEFORE_REFERENCES = 5
_DERIVER_LINE = 3


async def _closure(store_factory: InprocSessionFactory, root: StorePath) -> list[str]:
    async with store_factory() as session, session.store() as store:
        return [str(path) for path in await store.compute_fs_closure(root)]


async def test_dump_db_matches_the_nix_store_command(
    inproc_session: InprocSessionFactory,
    shared_nix_environment: NixTestEnvironment,
    seeded_store_path: StorePath,
) -> None:
    """The bytes must equal ``nix-store --dump-db`` over the same paths, in order."""
    closure = await _closure(inproc_session, seeded_store_path)
    async with inproc_session() as session, session.store() as store:
        ours = await store.dump_db(closure)

    result = await run_process(
        ["nix-store", "--store", shared_nix_environment.store_uri, "--dump-db", *closure],
    )
    assert result.returncode == 0, result.describe()
    assert ours == result.stdout


async def test_both_engines_produce_the_same_registration(
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
    seeded_store_path: StorePath,
) -> None:
    """The rpc engine must not lose the text on the way through the worker."""
    closure = await _closure(inproc_session, seeded_store_path)
    async with inproc_session() as session, session.store() as store:
        from_inproc = await store.dump_db(closure)
    async with rpc_session() as session, session.store() as store:
        from_rpc = await store.dump_db(closure)
    assert from_rpc == from_inproc
    assert from_rpc != ""


async def test_the_records_follow_the_order_of_the_argument(
    inproc_session: InprocSessionFactory,
    tmp_path: Path,
) -> None:
    """Reversing *paths* must reverse the records.

    The C++ parameter is a ``StorePathSet``, which sorts. One call for the
    whole sequence would therefore ignore the caller entirely, and this test
    fails on that implementation while the fidelity test above still passes.

    Two paths are added here rather than taken from ``seeded_store_path``,
    whose closure is one path. Order is not observable in a closure of one, so
    that fixture would leave this test skipped -- which is the same as absent,
    against exactly the implementation it exists to reject.
    """
    async with inproc_session() as session, session.store() as store:
        added: list[str] = []
        for index in (0, 1):
            source = anyio.Path(tmp_path) / f"ordered-{index}.txt"
            await source.write_text(f"nanopynix dump_db order fixture {index}\n")
            added.append(str(await store.add_to_store(str(source), name=f"dump-db-order-{index}", method="flat")))

        forward = await store.dump_db(added)
        backward = await store.dump_db(list(reversed(added)))

    assert forward != backward
    assert forward.splitlines()[0] == added[0]
    assert backward.splitlines()[0] == added[1]


async def test_show_hash_and_show_derivers_drop_their_fields(
    inproc_session: InprocSessionFactory,
    seeded_store_path: StorePath,
) -> None:
    """The two flags must remove exactly what ``nix-store`` removes."""
    async with inproc_session() as session, session.store() as store:
        full = await store.dump_db([seeded_store_path])
        without_hash = await store.dump_db([seeded_store_path], show_hash=False)
        without_deriver = await store.dump_db([seeded_store_path], show_derivers=False)

    full_lines = full.splitlines()
    assert len(full_lines) >= _LINES_BEFORE_REFERENCES
    # The hash and the size are one line each, and nothing else moves.
    assert without_hash.splitlines() == [full_lines[0], *full_lines[_DERIVER_LINE:]]
    assert without_deriver.splitlines()[_DERIVER_LINE] == ""


async def test_the_registration_makes_a_store_that_never_saw_the_paths_valid(
    inproc_session: InprocSessionFactory,
    shared_nix_environment: NixTestEnvironment,
    seeded_store_path: StorePath,
    tmp_path: Path,
) -> None:
    """``nix-store --load-db`` must accept the text and register the closure.

    This is the reason the binding exists, so the test does the whole trip: it
    builds a second store that holds the same bytes and no database, loads the
    registration into it, and asks Nix to check the paths. Copying the files
    by hand rather than with ``copy_closure`` is deliberate -- ``copy_closure``
    registers the paths itself, which would make the registration text do
    nothing and the test pass against an empty string.
    """
    closure = await _closure(inproc_session, seeded_store_path)
    async with inproc_session() as session, session.store() as store:
        registration = await store.dump_db(closure)

    destination = anyio.Path(tmp_path) / "target-store"
    await (destination / "nix/store").mkdir(parents=True)
    for path in closure:
        source = shared_nix_environment.physical_path(path)
        copy = await run_process(["cp", "-a", str(source), str(destination / "nix/store/")])
        assert copy.returncode == 0, copy.describe()

    registration_file = anyio.Path(tmp_path) / "registration"
    await registration_file.write_text(registration)

    # `root=` is what makes this a store of its own: the bytes live under
    # `destination`, and the paths stay the logical `/nix/store/...` names that
    # the registration text carries. Without it the URI would name the store of
    # the machine, and the test would ask Nix about paths that are already
    # valid there.
    target_uri = f"local?root={destination}"
    load = await run_process(
        ["sh", "-c", f"nix-store --store '{target_uri}' --load-db < '{registration_file}'"],
    )
    assert load.returncode == 0, load.describe()

    check = await run_process(
        ["nix-store", "--store", target_uri, "--check-validity", *closure],
    )
    assert check.returncode == 0, check.describe()

    requisites = await run_process(
        ["nix-store", "--store", target_uri, "--query", "--requisites", str(seeded_store_path)],
    )
    assert requisites.returncode == 0, requisites.describe()
    assert sorted(requisites.stdout.split()) == sorted(closure)
