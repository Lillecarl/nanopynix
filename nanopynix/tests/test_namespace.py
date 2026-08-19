"""Can a worker build in its own user namespace, against the host store?

``nanopynix.namespace`` puts a worker in a private user and mount namespace and
mounts an OverlayFS at ``/nix/store``: the host store below, a scratch
directory above. That buys three things at once -- builds see every path the
host already has without copying any of them, the host store cannot change, and
the worker owns the sandbox settings that the daemon otherwise controls.

The tests below split into two halves. The first half is pure and always runs:
the URI, the layout, and the two guards that stop the namespace being entered
at a moment when it cannot work. The second half needs a host that allows
unprivileged user namespaces and an OverlayFS over the filesystem holding the
layers, so it asks :func:`nanopynix.probe_namespace_support` first and skips
when the answer is no. That half builds a lower layer of its own under the
temporary directory of the test, and :func:`_lower_layer` gives the reason it
cannot use the store of the machine.

The pickling test is not filler. The spec reaches the worker as a pickled
argument through the forkserver, which is the only channel that works -- the
forkserver copies the environment once, when it starts, so an environment
variable set later never arrives. A spec that stopped pickling would break the
feature entirely and no other test here would notice.
"""

from __future__ import annotations

import pickle
import shutil
import sys
import threading
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

import anyio
import pytest

import nanopynix
from nanopynix.namespace import EXPERIMENTAL_FEATURE, STORE_DIR, enter_overlay_namespace
from nanopynix_testing.nix_markers import LINUX_NAMESPACES, LINUX_PROC_FS

# ── the pure half ───────────────────────────────────────────────────


class TestOverlayNamespaceSpec:
    def test_under_lays_the_four_directories_out(self, tmp_path: Path) -> None:
        spec = nanopynix.OverlayNamespace.under(tmp_path)
        assert spec.upper_dir == str(tmp_path / "upper")
        assert spec.work_dir == str(tmp_path / "work")
        assert spec.state_dir == str(tmp_path / "state")
        assert spec.log_dir == str(tmp_path / "log")

    def test_work_dir_is_not_inside_upper_dir(self, tmp_path: Path) -> None:
        """OverlayFS rejects a work directory inside the upper layer."""
        spec = nanopynix.OverlayNamespace.under(tmp_path)
        assert not spec.work_dir.startswith(spec.upper_dir + "/")

    def test_store_uri_names_every_layer(self, tmp_path: Path) -> None:
        spec = nanopynix.OverlayNamespace.under(tmp_path)
        uri = spec.store_uri()
        assert uri.startswith("local-overlay://?")
        assert f"upper-layer={spec.upper_dir}" in uri
        assert f"state={spec.state_dir}" in uri
        assert "lower-store=daemon" in uri

    def test_store_uri_escapes_a_path_that_would_break_the_query(self, tmp_path: Path) -> None:
        """A `&` in a path would otherwise read as another parameter."""
        spec = nanopynix.OverlayNamespace.under(tmp_path / "a&b")
        assert "&upper-layer=" in spec.store_uri()
        assert "a&b" not in spec.store_uri()

    def test_the_lower_layer_defaults_to_the_host_store(self, tmp_path: Path) -> None:
        """The default pair names one store, which is the production case.

        `local-overlay-store.cc:66` compares the `lowerdir` of the mount
        against the `realStoreDir` of the lower store, so a default that moved
        one of the two alone would give `mounted incorrectly`. Issue #208.
        """
        spec = nanopynix.OverlayNamespace.under(tmp_path)
        assert spec.lower_dir == STORE_DIR
        assert spec.lower_store == "daemon"

    def test_a_lower_layer_of_its_own_reaches_both_halves(self, tmp_path: Path) -> None:
        """A caller that moves the layer moves the store URI with it."""
        spec = nanopynix.OverlayNamespace.under(
            tmp_path, lower_store=f"local://?root={tmp_path}", lower_dir=str(tmp_path / "nix/store")
        )
        assert spec.lower_dir == str(tmp_path / "nix/store")
        # `store_config`, and not `store_uri`: the URI escapes the `?` and the
        # `=` of a nested store reference, so a comparison there would assert
        # the escaping rather than the value.
        assert spec.store_config().lower_store == f"local://?root={tmp_path}"

    def test_required_settings_disable_the_build_users_group(self, tmp_path: Path) -> None:
        """Root inside the namespace cannot chown to an unmapped group."""
        settings = nanopynix.OverlayNamespace.under(tmp_path).required_settings()
        assert settings["build-users-group"] == ""

    def test_the_spec_survives_a_round_trip_through_pickle(self, tmp_path: Path) -> None:
        spec = nanopynix.OverlayNamespace.under(tmp_path)
        restored = pickle.loads(pickle.dumps(spec))  # noqa: S301 -- round-tripping a value this test just serialised itself
        assert restored == spec


class TestEnterRefusesWhenItCannotWork:
    """Both guards fail *before* unsharing, so calling them here is safe."""

    # The platform guard runs before the thread guard, so off Linux this test
    # gets "needs Linux" and never reaches the message it asks for.
    @LINUX_NAMESPACES
    def test_a_second_thread_is_refused(self, tmp_path: Path) -> None:
        spec = nanopynix.OverlayNamespace.under(tmp_path)
        stop = threading.Event()
        thread = threading.Thread(target=stop.wait, daemon=True)
        thread.start()
        try:
            with pytest.raises(RuntimeError, match="single-threaded"):
                enter_overlay_namespace(spec)
        finally:
            stop.set()
            thread.join()


class TestSupportProbe:
    # `_mounts` is the measurement, and only Linux has the file it reads.
    @LINUX_PROC_FS
    def test_the_probe_answers_without_changing_this_process(self, tmp_path: Path) -> None:
        """Whatever the answer, the caller's own store must not move."""
        before = (STORE_DIR + " ") in _mounts()
        support = nanopynix.probe_namespace_support(tmp_path)
        assert isinstance(support.supported, bool)
        assert bool(support) is support.supported
        # A failed probe must say why, and a successful one must not invent one.
        assert bool(support.reason) is not support.supported
        assert ((STORE_DIR + " ") in _mounts()) is before

    def test_an_unsupported_answer_carries_a_reason(self) -> None:
        assert nanopynix.NamespaceSupport(False, "no user namespaces").reason
        assert not nanopynix.NamespaceSupport(True).reason


def _mounts() -> str:
    return Path("/proc/self/mounts").read_text(encoding="utf-8")


# ── a lower layer that the version under test makes ─────────────────


async def _lower_layer(root: Path, *, contents: Sequence[str] = ()) -> tuple[str, str]:
    """Make a lower layer under *root*, and return its URI and its directory.

    **The layer holds the closure of the worker as well as *contents*.** The
    worker runs out of the store that it overlays: its interpreter is
    ``sys.prefix``, which is a store path, and it imports a module *after* the
    mount. A layer that holds the closure of the builder alone takes the
    interpreter away from the running process, and the worker dies on
    ``ModuleNotFoundError: No module named 'anyio._backends'``. Measured, and
    issue #208 records the run.

    **The lower layer must not be the store of the machine.** That store
    belongs to whatever Nix installed it, and both routes to it carry that
    version: `lower-store=daemon` carries the worker protocol, and a direct
    `local` store carries the schema of `/nix/var/nix/db/db.sqlite`. A CI
    runner installs one Nix and each job of the matrix links another, so the
    tests below failed on the runner and passed on a developer machine, whose
    two versions agree. Issue #208 holds both measurements.

    A store under *root* has neither problem, because the Nix under test is
    the one that makes it.

    ``nix`` here is the Nix of this scope, and not the Nix of the machine.
    ``nix/suite-runtime.nix`` puts it first on PATH for the packaged runner
    and for the dev shell, and states the same reason. So the database this
    writes carries the schema that the linked libraries read back.
    """
    store_dir = root / STORE_DIR.lstrip("/")
    store_dir.mkdir(parents=True, exist_ok=True)
    uri = f"local://?root={root}"
    # `nix` as well as the interpreter. Nix resolves the `build-hook` setting
    # when a build starts, and that setting names the `nix` executable, so a
    # layer without it answers `Could not find executable 'nix'`.
    contents = [sys.prefix, _store_path_of("nix"), *contents]
    if contents:
        # `--no-check-sigs`, because the source is the store of this machine
        # and the destination is a directory this test just made.
        await anyio.run_process(
            [
                "nix",
                "--extra-experimental-features",
                "nix-command",
                "copy",
                "--no-check-sigs",
                "--to",
                uri,
                *contents,
            ]
        )
    return uri, str(store_dir)


# ── the half that needs a host that allows it ───────────────────────


@pytest.fixture(scope="module")
def namespace_support() -> nanopynix.NamespaceSupport:
    support = nanopynix.probe_namespace_support()
    if not support:
        pytest.skip(f"this host cannot run an overlay store worker: {support.reason}")
    return support


class TestNamespacedWorker:
    async def test_the_overlay_store_is_at_the_canonical_location(
        self, namespace_support: nanopynix.NamespaceSupport, tmp_path: Path
    ) -> None:
        """No ``root=``, so no relocation: paths are correct where they say.

        A relocated store would need ``nanopynix.store_exec_prefix`` to run
        anything out of it, which is the cost this design exists to avoid.
        The lower layer sits under ``tmp_path`` and the overlay still answers
        ``/nix/store``, because a lower layer is a source of bytes and not a
        location.
        """
        lower_uri, lower_dir = await _lower_layer(tmp_path / "lower")
        spec = nanopynix.OverlayNamespace.under(tmp_path, lower_store=lower_uri, lower_dir=lower_dir)
        async with nanopynix.rpc.Session(namespace=spec) as session, session.store() as store:
            dirs = await store.store_dirs()
            assert dirs.store_dir == STORE_DIR
            assert dirs.real_store_dir == STORE_DIR
            assert dirs.state_dir == spec.state_dir

    async def test_the_session_enables_the_experimental_feature(
        self, namespace_support: nanopynix.NamespaceSupport, tmp_path: Path
    ) -> None:
        """Nix refuses the store URI without it, so the session must add it."""
        lower_uri, lower_dir = await _lower_layer(tmp_path / "lower")
        spec = nanopynix.OverlayNamespace.under(tmp_path, lower_store=lower_uri, lower_dir=lower_dir)
        async with nanopynix.rpc.Session(namespace=spec) as session:
            assert session.namespace is spec
            # Opening the store at all proves the feature reached the worker.
            async with session.store() as store:
                assert await store.store_dirs()

    async def test_a_stdio_worker_enters_the_namespace_too(
        self, namespace_support: nanopynix.NamespaceSupport, tmp_path: Path
    ) -> None:
        """The spec reaches an exec'd worker, which can unpickle nothing.

        ``worker_start="stdio"`` carries the namespace as JSON on the command
        line instead -- see :mod:`nanopynix.rpc._worker_argv`. The store
        directories are the assertion, because they are what the mount
        changes: a worker that never entered the namespace would answer with
        the host's own state directory.
        """
        lower_uri, lower_dir = await _lower_layer(tmp_path / "lower")
        spec = nanopynix.OverlayNamespace.under(tmp_path, lower_store=lower_uri, lower_dir=lower_dir)
        async with (
            nanopynix.rpc.Session(
                namespace=spec,
                runtime_settings=nanopynix.NanopynixSettings(worker_start="stdio"),
            ) as session,
            session.store() as store,
        ):
            dirs = await store.store_dirs()
            assert dirs.store_dir == STORE_DIR
            assert dirs.state_dir == spec.state_dir

    async def test_a_build_lands_in_the_upper_layer_and_not_on_the_host(
        self,
        namespace_support: nanopynix.NamespaceSupport,
        tmp_path: Path,
        namespaced_derivation: Callable[[Path, str], Path],
    ) -> None:
        """The whole point, in one test.

        The build reads its builder through the lower layer, with nothing
        copied into the upper one, writes its result into the upper layer, and
        the lower layer never learns about it.

        Three things here are load-bearing, and each was learned the hard way.

        The derivation name carries a fresh value each run. An earlier version
        used a fixed name and promoted the result into the real host store, so
        the *second* run found it already valid through the lower layer, built
        nothing, and failed on an empty upper layer.

        The copy goes to a scratch store rather than to the store of the
        machine. Copying out of the overlay is what is under test, and the
        destination is not; a test that writes to the developer's own store on
        every run would leave a path behind each time, which is what caused
        the failure above.

        The lower layer holds the closure of the builder and nothing else, so
        this is also the assertion that a lower layer supplies bytes that the
        upper layer never copied. ``_lower_layer`` states why that layer is
        not the store of the machine.
        """
        bash = _host_bash()
        lower_uri, lower_dir = await _lower_layer(tmp_path / "lower", contents=[bash])
        spec = nanopynix.OverlayNamespace.under(tmp_path, lower_store=lower_uri, lower_dir=lower_dir)
        name = f"nanopynix-ns-build-{uuid.uuid4().hex[:12]}"
        destination = tmp_path / "destination"
        async with (
            nanopynix.rpc.Session(
                namespace=spec,
                # `substitute=False`, because the lower layer is the whole
                # world this build may read. Nix asks a substituter about the
                # output before it builds, and `/etc/ssl/certs/ca-certificates
                # .crt` points into the store that the overlay hides, so that
                # question fails on the trust anchors rather than answering
                # "not found". The build needs no substituter in any case.
                settings=nanopynix.NixSettings(sandbox="false", substitute=False, substituters=[]),
            ) as session,
            session.store() as store,
            session.store(lower_uri) as lower,
            session.store(f"local://?root={destination}") as scratch,
        ):
            nix_file = namespaced_derivation(tmp_path, name)
            async with session.eval(store) as evaluator:
                value = await evaluator.file(str(nix_file))
                outputs = await value.build()

            out = next(iter(outputs.values()))
            assert (Path(spec.upper_dir) / Path(out).name).exists(), "the result should be in the upper layer"
            assert not await lower.is_valid_path(out), "the lower layer should not have it"

            await store.copy_closure([out], scratch, check_sigs=False)
            assert await scratch.is_valid_path(out), "copy_closure should have promoted it out of the overlay"
            assert not await lower.is_valid_path(out), "the lower layer should still not have it"


@pytest.fixture
def namespaced_derivation() -> Callable[[Path, str], Path]:
    """Write a derivation whose builder is a *host* store path.

    ``builtins.storePath`` rather than a plain string: a string literal carries
    no context, so the builder would not enter ``inputSrcs`` and the sandbox
    would not mount it.
    """

    def build(directory: Path, name: str) -> Path:
        bash = _host_bash()
        nix_file = Path(directory) / f"{name}.nix"
        nix_file.write_text(
            f'let bash = builtins.storePath "{bash}";\n'
            f"in derivation {{\n"
            f'  name = "{name}";\n'
            f"  system = builtins.currentSystem;\n"
            f'  builder = "${{bash}}/bin/bash";\n'
            f'  args = [ "-c" "echo built-in-a-namespace > $out" ];\n'
            f"}}\n",
            encoding="utf-8",
        )
        return nix_file

    return build


def _host_bash() -> str:
    """The store path of a bash that is already in the host store."""
    return _store_path_of("bash")


def _store_path_of(tool: str) -> str:
    """The store path that holds *tool*, as PATH resolves it.

    A test skips when the tool is not in the store, because a lower layer is
    made of store paths and nothing else can go in one.
    """
    resolved = shutil.which(tool)
    if resolved is None:
        pytest.skip(f"no {tool} on PATH")
    real = Path(resolved).resolve()
    for parent in real.parents:
        if parent.parent == Path(STORE_DIR):
            return str(parent)
    pytest.skip(f"{tool} at {real} is not in {STORE_DIR}, so it cannot reach a lower layer here")


def test_the_feature_name_is_the_one_nix_expects() -> None:
    assert EXPERIMENTAL_FEATURE == "local-overlay-store"
