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
when the answer is no.

The pickling test is not filler. The spec reaches the worker as a pickled
argument through the forkserver, which is the only channel that works -- the
forkserver copies the environment once, when it starts, so an environment
variable set later never arrives. A spec that stopped pickling would break the
feature entirely and no other test here would notice.
"""

from __future__ import annotations

import pickle
import shutil
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

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
        """
        spec = nanopynix.OverlayNamespace.under(tmp_path)
        async with nanopynix.rpc.Session(namespace=spec) as session, session.store() as store:
            dirs = await store.store_dirs()
            assert dirs.store_dir == STORE_DIR
            assert dirs.real_store_dir == STORE_DIR
            assert dirs.state_dir == spec.state_dir

    async def test_the_session_enables_the_experimental_feature(
        self, namespace_support: nanopynix.NamespaceSupport, tmp_path: Path
    ) -> None:
        """Nix refuses the store URI without it, so the session must add it."""
        spec = nanopynix.OverlayNamespace.under(tmp_path)
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
        spec = nanopynix.OverlayNamespace.under(tmp_path)
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

        The build reads its builder out of the *host* store through the lower
        layer -- nothing was copied in -- writes its result into the upper
        layer, and the host store never learns about it.

        Two things here are load-bearing and were both learned the hard way.

        The derivation name carries a fresh value each run. An earlier version
        used a fixed name and promoted the result into the real host store, so
        the *second* run found it already valid through the lower layer, built
        nothing, and failed on an empty upper layer.

        The copy goes to a scratch store rather than to the daemon. Copying out
        of the overlay is what is under test, and the destination is not; a
        test that writes to the developer's own store on every run would leave
        a path behind each time, which is what caused the failure above.
        """
        spec = nanopynix.OverlayNamespace.under(tmp_path)
        name = f"nanopynix-ns-build-{uuid.uuid4().hex[:12]}"
        destination = tmp_path / "destination"
        async with (
            nanopynix.rpc.Session(namespace=spec, settings=nanopynix.NixSettings(sandbox="false")) as session,
            session.store() as store,
            session.store("daemon") as host,
            session.store(f"local://?root={destination}") as scratch,
        ):
            nix_file = namespaced_derivation(tmp_path, name)
            async with session.eval(store) as evaluator:
                value = await evaluator.file(str(nix_file))
                outputs = await value.build()

            out = next(iter(outputs.values()))
            assert (Path(spec.upper_dir) / Path(out).name).exists(), "the result should be in the upper layer"
            assert not await host.is_valid_path(out), "the host store should not have it"

            await store.copy_closure([out], scratch, check_sigs=False)
            assert await scratch.is_valid_path(out), "copy_closure should have promoted it out of the overlay"
            assert not await host.is_valid_path(out), "the host store should still not have it"


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
    resolved = shutil.which("bash")
    if resolved is None:
        pytest.skip("no bash on PATH to use as a builder")
    real = Path(resolved).resolve()
    for parent in real.parents:
        if parent.parent == Path(STORE_DIR):
            return str(parent)
    pytest.skip(f"bash at {real} is not in {STORE_DIR}, so it cannot be a builder here")


def test_the_feature_name_is_the_one_nix_expects() -> None:
    assert EXPERIMENTAL_FEATURE == "local-overlay-store"
