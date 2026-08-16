"""Typed store models: rendering, parsing, drift and version gates.

Every URI these tests assert on is checked against Nix's own
``StoreReference`` parser, so a passing test means Nix accepts the string, not
only that this library is self-consistent.

The two local store types are also opened for real. The SSH and S3 models
cannot be opened here, because there is no remote to open them against, so
their coverage is the drift check and the round-trip only. That is stated
rather than implied.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# The store bindings are C++ nanobind extensions without type stubs.

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import anyio
import pytest
from nanopynix_bindings import store as nanopynix_store
from pydantic import ValidationError

import nanopynix
from nanopynix import stores
from nanopynix.namespace import OverlayNamespace
from nanopynix.settings import NIX_2_34, NixStoreDefaults, field_is_supported, running_nix_version
from nanopynix_testing.nix_environment import force_rmtree
from nanopynix_testing.nix_markers import LINUX_PROC_FS
from test_support.notes import note

if TYPE_CHECKING:
    from pathlib import Path

    from nanopynix_testing.nix_environment import InprocSessionFactory, RpcSessionFactory


#: One instance of every model, covering the authority, the scheme override,
#: a list-valued setting and a setting whose value is itself a store URI.
CASES: list[stores.StoreConfig] = [
    stores.Auto(),
    stores.Auto(priority=9),
    stores.Dummy(),
    stores.Local(root="/tmp/x", require_sigs=False),
    stores.Local(root="/tmp/x", system_features=["big-parallel", "kvm"], trusted=True),
    stores.LocalOverlay(lower_store="daemon", upper_layer="/tmp/u", state="/tmp/s", log="/tmp/l"),
    stores.LocalOverlay(lower_store="unix:///run/nix/socket?max-connections=4", upper_layer="/tmp/u"),
    stores.Daemon(),
    stores.Daemon(socket="/run/nix/daemon-socket/socket", max_connections=4),
    stores.Ssh(host="user@build.example.com", compress=True, log_fd=2),
    stores.SshNg(host="user@build.example.com", remote_program=["nix", "daemon"], max_connection_age=60),
    stores.MountedSshNg(host="b.example.com", real="/nix/store", root="/mnt/remote"),
    stores.FileBinaryCache(path="/var/cache/nix", compression="zstd"),
    stores.HttpBinaryCache(host="cache.nixos.org"),
    stores.HttpBinaryCache(url_scheme="http", host="localhost:8080/nix", want_mass_query=True),
    stores.S3BinaryCache(bucket="my-bucket", region="eu-north-1", scheme="http", multipart_upload=True),
]


def _identifier(config: stores.StoreConfig) -> str:
    return f"{type(config).__name__}-{len(config.params())}"


@pytest.mark.parametrize("config", CASES, ids=_identifier)
def test_every_model_round_trips_through_nixs_own_parser(config: stores.StoreConfig) -> None:
    """Model to URI to model is the identity, and Nix accepts the URI.

    This is the property everything else rests on. Without it a store model is
    a second, divergent spelling of a Nix store rather than the same one.
    """
    uri = config.uri()
    note(**{f"uri/{_identifier(config)}": uri})

    reparsed = stores.parse(uri)

    assert type(reparsed) is type(config)
    assert reparsed == config, "parsing the rendered URI did not give the same configuration back"
    assert reparsed.uri() == uri, "the URI is not stable under a second render"


@pytest.mark.parametrize("config", CASES, ids=_identifier)
def test_nix_parses_every_rendered_uri(config: stores.StoreConfig) -> None:
    """Nix's ``StoreReference::parse`` accepts what these models render.

    Separate from the round-trip above because it fails differently: this
    catches a URI this library reads back happily and Nix refuses.
    """
    uri = config.uri()
    parsed: Any = nanopynix_store.parse_store_reference(uri)
    assert parsed["render"] == uri


def test_a_setting_the_store_type_does_not_have_is_refused() -> None:
    """A parameter Nix would silently ignore is an error here.

    ``upper-layer`` belongs to the overlay store. Nix drops it from a plain
    local store without a word, which is exactly the silence this library is
    built to remove.
    """
    with pytest.raises(ValueError, match="upper-layer") as excinfo:
        stores.parse("local://?upper-layer=/tmp/u")
    note(unknown_param_error=str(excinfo.value))


def test_an_unknown_scheme_names_the_schemes_that_exist() -> None:
    with pytest.raises(ValueError, match="no store model for scheme") as excinfo:
        stores.parse("nosuchscheme://x")
    assert "local" in str(excinfo.value), "the error should list the schemes that do work"


def test_a_required_authority_cannot_be_omitted() -> None:
    """An SSH store without a host is not a store, so the model refuses it."""
    with pytest.raises(ValidationError, match="host"):
        stores.Ssh()  # type: ignore[reportCallIssue] -- the missing argument is the point


def test_store_dir_is_not_the_global_store_setting() -> None:
    """Nix registers ``store`` twice, meaning two unrelated things.

    On a store it is the logical store directory. In ``globalConfig`` it is the
    URL of the store to use. Pydantic accepts a duplicate alias silently and
    sets both fields from one key, so keeping them apart is load-bearing.
    """
    config = stores.Local(store_dir="/custom/store")
    assert config.params() == {"store": "/custom/store"}
    assert "store" not in set(stores.StoreConfig.model_fields)

    settings = nanopynix.NixGlobalSettings(store="daemon")
    assert settings.to_worker_settings()["store"] == "daemon"
    assert "store-dir" not in settings.to_worker_settings()


def test_a_list_setting_survives_the_trip_through_a_uri() -> None:
    """Nix joins a list setting with spaces, and the parser splits it back."""
    config = stores.Local(system_features=["big-parallel", "kvm", "nixos-test"])
    assert config.params()["system-features"] == "big-parallel kvm nixos-test"

    reparsed = stores.parse(config.uri())
    assert isinstance(reparsed, stores.Local)
    assert reparsed.system_features == ["big-parallel", "kvm", "nixos-test"]


def test_a_nested_store_uri_survives_as_a_parameter_value() -> None:
    """An overlay store's lower store is itself a URI, with its own parameters.

    The nested ``?`` and ``=`` have to be escaped going in and unescaped coming
    out, and this is the case where getting that wrong is silent.
    """
    lower = stores.Daemon(socket="/run/nix/socket", max_connections=4).uri()
    config = stores.LocalOverlay(lower_store=lower, upper_layer="/tmp/u")

    reparsed = stores.parse(config.uri())
    assert isinstance(reparsed, stores.LocalOverlay)
    assert reparsed.lower_store == lower


# ── Drift against the Nix this build is linked with ──────────────────


def test_every_store_model_matches_nixs_registry() -> None:
    """The models and Nix's store registry name the same settings.

    Nix's registry is built by a static initialiser per linked store
    implementation, so this checks what this build can open rather than what
    Nix documents.
    """
    drift = stores.check_all_store_model_drift()
    note(nix_version=running_nix_version())
    for name, result in drift.items():
        note(**{f"drift/{name}": {"missing": result.missing, "extra": result.extra}})
    offenders = {name: result for name, result in drift.items() if not result.ok}
    assert not offenders, f"store models drifted from Nix's registry: {offenders}"


#: The store types Nix itself ships, pinned so that dropping one is an error.
#: The registry also holds anything :func:`register_store_implementation` added,
#: which the suite does, so the two sets are not equal and must not be compared.
NIX_BUILTIN_STORE_TYPES = frozenset(
    {
        "Dummy Store",
        "Experimental Local Overlay Store",
        "Experimental SSH Store",
        "Experimental SSH Store with filesystem mounted",
        "HTTP Binary Cache Store",
        "Local Binary Cache Store",
        "Local Daemon Store",
        "Local Store",
        "S3 Binary Cache Store",
        "SSH Store",
    },
)


def test_every_store_type_nix_ships_has_a_model() -> None:
    """Nix's own store types are all modelled, and every model names a real one.

    Not an equality against the whole registry. That registry is process-global
    and grows: ``register_store_implementation`` adds a type permanently, and
    the suite registers several. So this checks the two directions that are
    actually true.

    A store type added by a *future* Nix is the known blind spot here. It shows
    up as an unmodelled name in the note below rather than as a failure,
    because this process cannot tell it apart from one the suite registered.
    """
    registered = set(stores.list_store_types())
    modelled = {model.store_type_name for model in stores.STORE_MODELS} - {None}

    note(unmodelled=sorted(registered - modelled - NIX_BUILTIN_STORE_TYPES))
    assert modelled <= registered, "a model names a store type this build does not register"
    assert modelled == NIX_BUILTIN_STORE_TYPES, "the models and Nix's own store types diverged"
    assert registered >= NIX_BUILTIN_STORE_TYPES, "this build does not register a store type Nix ships"


def test_auto_has_no_registry_entry_and_says_so() -> None:
    """``auto`` is resolved by ``openStore``, not implemented as a store type."""
    assert stores.Auto.store_type_name is None
    assert stores.check_store_model_drift(stores.Auto).ok
    assert stores.Auto().uri() == "auto"


# ── Version gates ────────────────────────────────────────────────────


def test_a_setting_this_nix_lacks_raises_rather_than_rendering() -> None:
    """A version-gated field is refused loudly on a Nix that has no such setting.

    Rendering it anyway would put a parameter in the URI that Nix ignores.
    """
    running = running_nix_version()
    field = stores.S3BinaryCache.model_fields["buffer_size"]
    supported = field_is_supported(field)
    note(running_nix=running, buffer_size_supported=supported)

    config = stores.S3BinaryCache(bucket="b", buffer_size=1024)
    if supported:
        assert config.params()["buffer-size"] == "1024"
    else:
        with pytest.raises(ValueError, match=r"buffer-size .*removed in Nix"):
            config.uri()


def test_the_gates_bound_a_field_from_both_sides() -> None:
    """``multipart-chunk-size`` and ``buffer-size`` are the same setting, renamed.

    Exactly one of them is available on any supported Nix, which is what the
    two gates together express.
    """
    fields = stores.S3BinaryCache.model_fields
    new_name = field_is_supported(fields["multipart_chunk_size"])
    old_name = field_is_supported(fields["buffer_size"])
    note(multipart_chunk_size=new_name, buffer_size=old_name, boundary=NIX_2_34)
    assert new_name != old_name, "the rename boundary should make exactly one of the two names live"


# ── Session defaults, and the overlay store ──────────────────────────


def test_session_store_defaults_go_under_the_store_that_names_them() -> None:
    """A session default reaches the URI, and a value on the store beats it.

    Nix has no global for these four settings, so rendering them into each
    store's URI is the only way a session-wide default can mean anything.
    """
    defaults = NixStoreDefaults(priority=5, trusted=True, want_mass_query=True)

    filled = stores.resolve_store_spec(stores.Local(root="/tmp/x"), defaults)
    assert stores.parse(filled) == stores.Local(
        root="/tmp/x",
        priority=5,
        trusted=True,
        want_mass_query=True,
    )

    overridden = stores.resolve_store_spec(stores.Local(root="/tmp/x", priority=99), defaults)
    parsed = stores.parse(overridden)
    assert isinstance(parsed, stores.Local)
    assert parsed.priority == 99, "a value set on the store must beat the session default"


def test_a_uri_string_is_never_rewritten() -> None:
    """A URI a caller wrote by hand reaches Nix exactly as written.

    Merging a default into it would mean reparsing and re-rendering someone
    else's string, and a store URI is too load-bearing to rewrite quietly.
    """
    defaults = NixStoreDefaults(priority=5)
    assert stores.resolve_store_spec("local://?root=/tmp/x", defaults) == "local://?root=/tmp/x"


# ── Resolving `auto` ─────────────────────────────────────────────────


def test_an_auto_that_became_the_daemon_is_reopened_with_a_pool() -> None:
    """The connection limit reaches the store `auto` turned into.

    Nix gives a daemon store one connection, and a library whose callers run
    store operations at the same time needs more. ``auto`` itself cannot carry
    the setting, because a parameter on it turns off Nix's rootless chroot
    store, so the limit goes on a second open.
    """
    reopen = stores.resolve_auto_uri("auto", "daemon")

    assert reopen is not None
    assert stores.parse(reopen) == stores.Daemon(max_connections=stores.DAEMON_MAX_CONNECTIONS)


def test_a_socket_that_is_not_the_default_one_survives_the_reopen() -> None:
    """The second open goes to the same daemon as the first one.

    The reopen is built from what Nix reported, and not from the bare default,
    so a host with ``NIX_DAEMON_SOCKET_PATH`` set keeps its socket.
    """
    reopen = stores.resolve_auto_uri("auto", "unix:///run/other/socket")

    assert reopen is not None
    parsed = stores.parse(reopen)
    assert isinstance(parsed, stores.Daemon)
    assert parsed.socket == "/run/other/socket"
    assert parsed.max_connections == stores.DAEMON_MAX_CONNECTIONS


def test_a_named_daemon_store_gets_the_pool_when_it_is_opened() -> None:
    """The two ways to reach the daemon agree, and neither is the model itself.

    ``Daemon()`` describes a URI that states no limit, so it renders none. The
    number arrives when the store is about to be opened, which is what
    ``resolve_store_spec`` does. ``test_stores_properties.py`` is why the model
    cannot carry it.
    """
    assert stores.Daemon().uri() == "unix://"
    assert stores.resolve_store_spec(stores.Daemon()) == f"unix://?max-connections={stores.DAEMON_MAX_CONNECTIONS}"
    assert stores.resolve_store_spec(stores.Daemon(max_connections=1)) == "unix://?max-connections=1"
    assert stores.resolve_store_spec(stores.Local(root="/tmp/x")) == "local://?root=/tmp/x"


@pytest.mark.parametrize(
    ("requested", "resolved", "why"),
    [
        ("auto", "local", "a local store has no connections to pool"),
        ("auto", "dummy://", "no other store type is what `auto` is about"),
        ("auto", "moon://gouda", "a store type with no model says nothing about this rule"),
        ("daemon", "daemon", "a caller who named the daemon gets the daemon they named"),
        ("unix://?max-connections=2", "daemon", "a stated limit is the caller's own"),
    ],
)
def test_a_store_that_is_left_alone(requested: str, resolved: str, why: str) -> None:
    """Only an `auto` that became the daemon is opened a second time."""
    assert stores.resolve_auto_uri(requested, resolved) is None, why


def test_the_overlay_namespace_renders_through_the_store_model(tmp_path: Path) -> None:
    """``OverlayNamespace`` names a store, and the store model spells it.

    ``work_dir`` is deliberately absent from the URI. OverlayFS needs it; Nix
    does not, because it is a requirement of the mount rather than a setting.
    """
    namespace = OverlayNamespace.under(tmp_path)
    config = namespace.store_config()
    note(overlay_uri=namespace.store_uri())

    assert isinstance(config, stores.LocalOverlay)
    assert config.upper_layer == namespace.upper_dir
    assert config.state == namespace.state_dir
    assert config.log == namespace.log_dir
    assert config.lower_store == namespace.lower_store
    assert namespace.work_dir not in namespace.store_uri()

    assert stores.parse(namespace.store_uri()) == config


# ── Opening a real store ─────────────────────────────────────────────


async def test_a_model_opens_the_store_it_describes(tmp_path: Path) -> None:
    """The two local models are opened for real, not only rendered.

    The SSH and S3 models have no remote to answer them here, so their
    coverage stops at the drift check and the round-trip above.
    """
    config = stores.Local(root=str(tmp_path / "store"), require_sigs=False)
    store = nanopynix.open_store(config.uri())
    note(local_store_dir=store.get_store_dir())
    assert store.get_store_dir() == "/nix/store"

    dummy = nanopynix.open_store(stores.Dummy().uri())
    assert dummy.get_store_dir() == "/nix/store"


async def test_both_engines_open_auto_the_way_resolve_auto_uri_asks(
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> None:
    """`resolve_auto_uri` reaches the store, and it reaches it on both engines.

    ``CoreRuntime.open_store`` is the one place that applies the rule, and both
    engines call it, so this asserts the contract rather than one outcome.
    Which outcome the host gives is not fixed: a developer on a multi-user
    install sees the daemon, and this suite's own state directory is writable,
    so ``auto`` becomes a local store under it. Both readings are checked
    against what the rule says for the store Nix reported.

    Opening ``auto`` costs about half a millisecond and opens no socket,
    because a connection pool is lazy. Nothing here writes to the host store.

    **The check needs a Nix that can report a store's own parameters, and 2.31
    cannot.** There, ``UDSRemoteStoreConfig::getReference`` builds its answer
    with no ``.params`` at all -- ``getQueryParams`` arrived later -- so
    ``render(withParams=true)`` says ``daemon`` on a store that really does
    carry the limit. The rule still runs on 2.31, and no reading of the store
    can show it, so the probe below decides whether there is anything to
    assert. A version number would say the same thing and would rot.
    """
    for label, factory in (("inproc", inproc_session), ("rpc", rpc_session)):
        async with factory() as nix:
            async with nix.store(stores.Dummy(priority=7)) as probe:
                reports_params = "priority=7" in await probe.uri(with_params=True)
            async with nix.store("auto") as store:
                plain = await store.uri()
                full = await store.uri(with_params=True)
        note(**{f"auto_{label}": full, f"reports_params_{label}": reports_params})

        if not reports_params:
            pytest.skip("this Nix does not report a store's own parameters, so the reopen cannot be read back")

        expected = stores.resolve_auto_uri("auto", plain)
        if expected is None:
            assert "max-connections" not in full, f"{label} added a limit the rule did not ask for"
        else:
            assert stores.parse(full) == stores.parse(expected), f"{label} did not apply the rule"


# ── One LocalStore for each state directory ──────────────────────────


async def _write_store_root(tmp_path: Path, name: str = "store") -> tuple[str, anyio.Path, anyio.Path]:
    """Make an empty store root and a small file to add to it."""
    root = anyio.Path(tmp_path) / name
    await root.mkdir()
    source = anyio.Path(tmp_path) / "payload.txt"
    if not await source.exists():
        await source.write_text("one local store for each state directory\n")
    return stores.Local(root=str(root)).uri(), root, source


async def _temp_roots_descriptors(state_dir: anyio.Path) -> list[str]:
    """Every descriptor of this process on a file in the temp-roots directory.

    One `LocalStore` makes one such file and holds it open for its whole life,
    so the length of this list is the number of `LocalStore` objects that this
    process has for the state directory. That is true of every supported Nix,
    and the **name** of the file is not:

    - 2.34 and 2.35 call it `<stateDir>/temproots/<getpid()>`, so two stores
      in one process compete for one name.
    - Nix git calls `makeTempPath`, which gives each store its own name.
      Upstream removed the assumption there, and the count still reports what
      this asks.

    A deleted file keeps its descriptor, and Linux writes " (deleted)" after
    the name, so a stale one is in the list and is easy to tell apart.
    """
    # Resolved, because a link in `/proc/self/fd` names the real path and Nix
    # git calls `canonPath(root, true)` on this directory as well. A workspace
    # under a symlink makes the two spellings differ otherwise.
    wanted = str(await (state_dir / "temproots").resolve())
    held: list[str] = []
    async for entry in anyio.Path("/proc/self/fd").iterdir():
        try:
            target = str(await entry.readlink())
        except OSError:
            continue
        if target.startswith(wanted + "/"):
            held.append(target)
    return held


@LINUX_PROC_FS
async def test_two_handles_on_one_local_store_share_the_temp_roots_file(
    inproc_session: InprocSessionFactory,
    tmp_path: Path,
) -> None:
    """Two `Store` objects on one URI hold one temp-roots file between them.

    One descriptor means one `LocalStore`, which is what the cache in
    `nix_store.cpp` gives. Two means two, and on 2.34 and 2.35 two is a
    defect: those versions name the file `<stateDir>/temproots/<getpid()>`,
    and Nix gives the assumption in its own comment. The file "*must* be
    stale, since there can be no two processes with the same pid". A second
    `LocalStore` in one process breaks that in two ways:

    - The two race between `pathExists` and `openLockFile`. When both open the
      same inode, the second one waits in `flock` for ever, because the first
      one never gives the lock up. `lockFile` looks for an interrupt only
      after `flock` returns, so nothing cancels that wait. Issue #99.
    - When the two do not race, the second one calls `tryUnlink` and removes
      the file of the first one. The temporary roots of the first store are
      then invisible to the garbage collector, which may delete a path that
      the store is using.

    The second failure is the one this test drives, because it is
    deterministic: the two stores take their roots one after the other, so
    the second store always reaches `tryUnlink`. Two descriptors, one of them
    on a deleted file, is the broken answer there.

    Nix git gives each store its own name, so neither failure can happen on
    it. The count is still one, because the cache still gives one store, and
    that is what this asserts on every version.
    """
    uri, root, source = await _write_store_root(tmp_path)
    state_dir = root / "nix" / "var" / "nix"

    async with inproc_session() as nix, nix.store(uri) as first, nix.store(uri) as second:
        # `add_to_store` takes a temporary root, which is what makes the
        # file. The order is deliberate, and it is not a race.
        await first.add_to_store(str(source), name="first")
        await second.add_to_store(str(source), name="second")

        held = await _temp_roots_descriptors(state_dir)
        note(temp_roots_descriptors=held)
        assert held, "no descriptor holds the temp-roots file, so this test proves nothing"
        assert len(held) == 1, f"{len(held)} LocalStore objects, each with its own temp-roots file: {held}"


async def test_a_store_root_that_came_back_is_not_the_old_store(
    inproc_session: InprocSessionFactory,
    tmp_path: Path,
) -> None:
    """A path is not an identity, so the cache does not key on one.

    pytest's `tmp_path_factory` numbers a directory from the highest one that
    exists, and the store fixture of this suite removes its root. The next
    test therefore gets the same name, and one URI names two different
    directories in one session. A cache on the path returned the store of the
    first test, which held a directory that no longer exists, and four tests
    of `tests/rpc/test_l3_inproc.py` failed on "No such file or directory".

    This removes the root while the first store is still alive, which is what
    makes the entry stale rather than absent. The second store must be a new
    one, and it must work.
    """
    uri, root, source = await _write_store_root(tmp_path)

    async with inproc_session() as nix:
        async with nix.store(uri) as first:
            await first.add_to_store(str(source), name="before")

            await force_rmtree(tmp_path / "store")
            await root.mkdir()

            async with nix.store(uri) as second:
                # This raised `SysError: opening file ... No such file or
                # directory` when the cache keyed on the URI alone.
                path = await second.add_to_store(str(source), name="after")
        note(after_recreation=path)


@LINUX_PROC_FS
async def test_two_store_roots_get_a_local_store_each(
    inproc_session: InprocSessionFactory,
    tmp_path: Path,
) -> None:
    """The cache shares one store, and it does not join two of them.

    Each state directory holds its own temp-roots file, so two roots give two
    files and each file has one descriptor. A cache that returned one store
    for both roots would leave the second directory with no file at all, and
    the second store would write its temporary roots into the first store.
    """
    first_uri, first_root, source = await _write_store_root(tmp_path, "one")
    second_uri, second_root, _ = await _write_store_root(tmp_path, "two")

    async with inproc_session() as nix, nix.store(first_uri) as first, nix.store(second_uri) as second:
        await first.add_to_store(str(source), name="in-one")
        await second.add_to_store(str(source), name="in-two")

        held_first = await _temp_roots_descriptors(first_root / "nix" / "var" / "nix")
        held_second = await _temp_roots_descriptors(second_root / "nix" / "var" / "nix")
        note(temp_roots_one=held_first, temp_roots_two=held_second)
        assert len(held_first) == 1, f"the first root has {len(held_first)} temp-roots descriptors: {held_first}"
        assert len(held_second) == 1, f"the second root has {len(held_second)} temp-roots descriptors: {held_second}"
        assert held_first != held_second, "both roots reported the same file, so the two stores were joined"
