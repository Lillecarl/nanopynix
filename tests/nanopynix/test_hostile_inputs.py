"""What each entry point does with input that is wrong (#20).

``_require_filesystem_path`` in ``_core/_objects.py`` already treats the empty
string this way: one documented answer, given deliberately, rather than
whatever the layer below happens to do. This module extends that treatment to
the rest of the bad inputs a caller can supply, and records what each one
answers today.

**The subject is the class and the wording, not the fact that it fails.** Every
input here already fails somehow. What matters is whether the failure names a
cause the caller can act on, and whether two inputs that are wrong in the same
way report it the same way. Three answers were wrong, and this module
corrects all three:

1. :func:`nanopynix.stores.parse` promises ``ValueError`` and delivered it for
   an unknown scheme and for an unknown parameter -- but leaked Nix's own
   ``UsageError``, a ``RuntimeError`` carrying terminal colour codes, when Nix
   could not read the URI at all. Which half of the URI was at fault chose the
   exception class.
2. The worker answered an evaluator handle it never issued with "no EvalState
   is open — call OpenEval before evaluating". One had been opened. The
   message named the wrong cause and dropped the handle.
3. A settings value Nix refuses made ``Session.__aenter__`` raise, and Python
   does not run ``__aexit__`` for that. The worker process, its channel and
   its log task all stayed behind. ``WorkerClient.open`` now closes what it
   opened before it raises.

**What this module does not claim.** It does not assert that an error message
is *good*, only that it names the subject. It does not cover a hostile Nix
expression -- that is evaluation, and ``test_error_boundaries.py`` owns it.

Two answers are recorded as they are rather than corrected, each for a stated
reason. See ``test_an_unknown_value_handle_still_answers_with_a_bare_key_error``
and ``test_an_unknown_experimental_feature_is_a_bare_runtime_error``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import pytest
from nanopynix_proto.nix.eval import AttrRequest

import nanopynix
from nanopynix import stores
from nanopynix.exceptions import EvalSessionClosedError
from tests.support.notes import note

if TYPE_CHECKING:
    from tests.support.nix_environment import NixTestEnvironment

#: A handle no worker issues. Handle 0 is the wire's "none", so it is a
#: different case and has its own test.
ABSENT_HANDLE = 999_999


# ── Store URIs ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "uri",
    ["!!!", "file:///a b", "file:///a%zz", "https://[", "file://relative"],
    ids=["not-a-uri", "space", "bad-percent", "unterminated-ipv6", "relative-file"],
)
def test_a_uri_nix_cannot_read_is_a_value_error(uri: str) -> None:
    """The correction. Each of these five raised ``UsageError`` before.

    ``UsageError`` is a ``RuntimeError`` from the compiled bindings, so a
    caller following this module's documented contract caught nothing. The
    message also carried Nix's terminal colour codes, which is what a console
    wants and not what an exception should hold.
    """
    with pytest.raises(ValueError, match="Nix cannot read") as excinfo:
        stores.parse(uri)

    message = str(excinfo.value)
    note(**{f"parse/{uri}": message})
    assert uri in message, "the message must name the URI that was refused"
    assert "\x1b[" not in message, "Nix's colour codes must not reach the exception"


def test_a_rendered_uri_nix_cannot_read_is_a_value_error() -> None:
    """The same rule on the way out, where the fields make the bad URI.

    ``uri()`` is where a caller meets this: nothing rejects an authority at
    construction, so the model is valid and the URI it renders is not.
    """
    with pytest.raises(ValueError, match="Nix cannot read") as excinfo:
        stores.FileBinaryCache(path="relative").uri()
    assert "file://relative" in str(excinfo.value)


def test_every_bad_uri_reports_the_same_class() -> None:
    """One class for three different faults, which is the point of the fix.

    An unknown scheme and an unknown parameter were already ``ValueError``.
    A URI Nix cannot read was not, so a caller had to catch two unrelated
    classes to handle "this URI is bad".
    """
    faults = {
        "unreadable": "!!!",
        "unknown-scheme": "nosuchscheme://x",
        "unknown-parameter": "local://?upper-layer=/tmp/u",
        "bad-parameter-type": "local://?priority=abc",
    }
    for label, uri in faults.items():
        with pytest.raises(ValueError) as excinfo:  # noqa: PT011 -- the class is the assertion
            stores.parse(uri)
        note(**{f"fault/{label}": type(excinfo.value).__name__})
        assert type(excinfo.value) is ValueError, f"{label} raised a subclass, so `except ValueError` is not enough"


def test_the_empty_uri_is_auto_and_not_an_error() -> None:
    """Nix reads ``""`` as the automatic store, so this module must too.

    Recorded because it looks like a hostile input and is not. Rejecting it
    would make nanopynix stricter than Nix for no gain, and this test is what
    stops someone adding that rejection while reading the tests above.
    """
    assert stores.parse("") == stores.Auto()


# ── Settings ─────────────────────────────────────────────────────────


def test_a_settings_value_of_the_wrong_type_is_refused_before_nix_sees_it() -> None:
    """Pydantic rejects it at construction, which is the earliest possible.

    A string in an integer field never reaches a worker, so no session has to
    be started and torn down to learn that the caller made a typing mistake.
    """
    with pytest.raises(ValueError, match="max_jobs") as excinfo:
        nanopynix.NixGlobalSettings(max_jobs="not-a-number")  # type: ignore[arg-type] -- the wrong type is the point
    note(wrong_type=str(excinfo.value)[:120])

    with pytest.raises(ValueError, match="no_such_setting"):
        nanopynix.NixGlobalSettings(no_such_setting=1)  # type: ignore[call-arg] -- the unknown name is the point


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [("max_jobs", -5, "max-jobs"), ("cores", 10**12, "cores")],
    ids=["negative-max-jobs", "cores-too-large"],
)
async def test_a_settings_value_nix_rejects_names_the_setting(
    shared_nix_environment: NixTestEnvironment,
    field: str,
    value: int,
    expected: str,
) -> None:
    """Pydantic accepts the type, and Nix refuses the value.

    This is the half pydantic cannot check: ``-5`` is an integer and
    ``max-jobs`` is an integer setting, but Nix wants ``auto`` or a
    non-negative one. The failure must still name the setting, because the
    caller passed a whole settings object and needs to know which field.

    **The assertion is the message, not the class, and that is measured.** On
    Nix 2.34 both of these arrive as :class:`nanopynix.NixError`. On 2.31
    ``cores`` arrives as a raw ``nanopynix_bindings.errors.UsageError`` while
    ``max-jobs`` still translates -- the two are refused at different moments,
    and only one of those moments is inside the translating call. Asserting
    the class would therefore pin a version difference that has nothing to do
    with what this test is for. The setting's name is in the message on every
    supported version, and that is what a caller needs.
    """
    base = shared_nix_environment.settings.model_dump(exclude_none=True)
    # `model_validate` rather than keyword arguments: a `**` splat of a dict
    # whose values are `Any` makes pyright check it against all 100+ fields.
    settings = nanopynix.NixSettings.model_validate({**base, field: value})

    with pytest.raises(Exception) as excinfo:  # noqa: PT011 -- see the docstring: the class differs by Nix version
        async with (
            nanopynix.rpc.Session(
                store_uri=shared_nix_environment.store_uri,
                load_config=False,
                settings=settings,
            ) as session,
            session.store(),
        ):
            pass

    message = str(excinfo.value)
    note(**{f"rejected/{field}": f"{type(excinfo.value).__module__}.{type(excinfo.value).__name__}: {message[:120]}"})
    assert expected in message, "the failure must name the setting Nix refused"


async def test_a_refused_setting_leaves_no_worker_behind(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """The second correction. A failed open used to leak the whole worker.

    ``Session.__aenter__`` calls ``WorkerClient.open``, which spawns the
    process, opens the channel, starts the log task, and only then asks Nix to
    take the settings. Nix refuses, ``open`` raises, and Python does not run
    ``__aexit__`` for an ``__aenter__`` that raised -- so nothing closed any of
    the three. The worker stayed alive holding a Nix store connection that
    nothing in this process remembered.

    The leak showed itself first as an intermittent failure of the test above:
    the abandoned channel raised ``StreamTerminatedError`` in a task nobody
    awaited, and whether the garbage collector noticed before the test ended
    decided which test the report landed on.
    """
    base = shared_nix_environment.settings.model_dump(exclude_none=True)
    settings = nanopynix.NixSettings.model_validate({**base, "cores": 10**12})

    session = nanopynix.rpc.Session(
        store_uri=shared_nix_environment.store_uri,
        load_config=False,
        settings=settings,
    )
    manager: Any = session._manager  # type: ignore[reportPrivateUsage] -- the leak is worker state, and only the manager holds it

    with pytest.raises(Exception, match="cores"):
        await session.open()

    pid = manager._worker_pid
    note(worker_pid=pid)
    assert pid is not None, "the worker did start, so the test is measuring the case it means to"
    assert manager._channel is None, "the channel must be closed"
    assert manager._worker_proc is None, "the process handle must be released"
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_an_unknown_experimental_feature_is_a_bare_runtime_error(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """Recorded as it is, and not corrected here. The reason is the contract.

    A bad ``max-jobs`` above arrives as :class:`nanopynix.NixError`, because
    Nix raises it and the translation layer catches it. This one is a
    ``std::runtime_error`` thrown by ``nix_util.cpp`` itself before Nix is
    asked, so nothing translates it -- the caller gets a bare ``RuntimeError``
    for one bad setting and a ``NixError`` for another.

    That asymmetry is worth removing, and the removal belongs in the bindings
    rather than here: ``tests/nanopynix/bindings/test_util.py`` pins the
    ``RuntimeError`` as ``enable_experimental_feature``'s own contract, so
    changing it is a change to that binding and to that test. Pinned here so
    the inconsistency is written down where a caller meets it.
    """
    base = shared_nix_environment.settings.model_dump(exclude_none=True)
    settings = nanopynix.NixSettings.model_validate({**base, "experimental_features": ["no-such-feature"]})

    with pytest.raises(RuntimeError, match="unknown experimental feature") as excinfo:
        async with (
            nanopynix.rpc.Session(
                store_uri=shared_nix_environment.store_uri,
                load_config=False,
                settings=settings,
            ) as session,
            session.store(),
        ):
            pass

    assert not isinstance(excinfo.value, nanopynix.NixError), (
        "if this now translates, delete the test and the paragraph in the module docstring"
    )


# ── Handles the worker never issued ──────────────────────────────────


def _eval_proxy(evaluator: Any) -> Any:
    """The RPC proxy behind an evaluator, which is what carries the handles."""
    proxy: Any = evaluator._proxy  # type: ignore[reportPrivateUsage] -- a hand-built request is the only way to reach this path
    return proxy


async def test_an_unknown_evaluator_handle_names_the_handle(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """The correction. This said "no EvalState is open" while one was open.

    No client sends this, because the client fills the handle in itself. The
    message matters anyway: it is what a person debugging a worker sees, and
    it used to send them to look for a missing ``OpenEval`` that was not
    missing.
    """
    async with (
        shared_nix_environment.rpc_session() as session,
        session.store() as store,
        session.eval(store) as evaluator,
    ):
        proxy = _eval_proxy(evaluator)
        request = AttrRequest(eval_handle=ABSENT_HANDLE, handle=1, name="a")

        with pytest.raises(RuntimeError) as excinfo:
            await proxy._worker.invoke(proxy._worker.eval_stub.attr, request, timeout=10.0)  # type: ignore[reportPrivateUsage] -- see above

        message = str(excinfo.value)
        note(unknown_eval_handle=message)
        assert str(ABSENT_HANDLE) in message, "the message must name the handle that was refused"
        assert "call OpenEval" not in message, "an evaluator is open, so this must not blame a missing OpenEval"


async def test_handle_zero_still_says_to_open_an_evaluator(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """The other half of the same fix, which must keep its old answer.

    Zero is the wire's "none", so this caller really did skip ``OpenEval``.
    Separating the two is the whole change; a test on only the new branch
    would let someone collapse them again.
    """
    async with (
        shared_nix_environment.rpc_session() as session,
        session.store() as store,
        session.eval(store) as evaluator,
    ):
        proxy = _eval_proxy(evaluator)

        with pytest.raises(RuntimeError, match="call OpenEval"):
            await proxy._worker.invoke(  # type: ignore[reportPrivateUsage] -- see above
                proxy._worker.eval_stub.attr,
                AttrRequest(eval_handle=0, handle=1, name="a"),
                timeout=10.0,
            )


async def test_an_unknown_value_handle_still_answers_with_a_bare_key_error(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """Recorded as it is. The handle is named, and the class is a builtin.

    ``HandleRegistry.get`` raises ``KeyError``, which crosses the wire and
    arrives as ``KeyError`` again. The message does name the handle, so a
    person debugging is not misled -- unlike the evaluator case above, which
    is why that one was corrected and this one is not.

    What is wrong here is smaller and wider: a ``KeyError`` is not a
    nanopynix exception, and ``KeyError.__str__`` reprs its argument, so the
    text arrives wrapped in quotes it did not start with. Changing the class
    means changing what crosses boundary B and updating ``WIRE_CLASSES`` in
    ``test_exceptions_classify.py``, which is a wider change than this issue.
    Pinned so that change is deliberate when it comes.
    """
    async with (
        shared_nix_environment.rpc_session() as session,
        session.store() as store,
        session.eval(store) as evaluator,
    ):
        proxy = _eval_proxy(evaluator)

        with pytest.raises(KeyError) as excinfo:
            await proxy._worker.invoke(  # type: ignore[reportPrivateUsage] -- see above
                proxy._worker.eval_stub.attr,
                AttrRequest(eval_handle=proxy._eval_handle, handle=ABSENT_HANDLE, name="a"),
                timeout=10.0,
            )

        note(unknown_value_handle=str(excinfo.value))
        assert str(ABSENT_HANDLE) in str(excinfo.value), "the message must name the handle"


# ── A proxy that outlives what it points at ──────────────────────────


async def test_a_value_proxy_says_so_after_its_evaluator_closes(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """The handle is gone, and the proxy must say that rather than guess.

    Both scopes are tested because they release the handle by different
    routes: closing the evaluator releases every value it rooted, and closing
    the session tears down the worker under it.
    """
    async with shared_nix_environment.rpc_session() as session, session.store() as store:
        async with session.eval(store) as evaluator:
            root = await evaluator.string("{ a = 1; }")
            assert await root.attr("a").as_int() == 1, "the proxy must work while its evaluator is open"

        with pytest.raises(EvalSessionClosedError, match="closed"):
            await root.attr("a").as_int()

    with pytest.raises(EvalSessionClosedError, match="closed"):
        await root.attr("a").as_int()


async def test_a_value_proxy_says_so_after_the_worker_dies(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """A killed worker, which is not an orderly close and must not look like one.

    The issue asks for this case separately from the one above, and it is the
    harder one: nothing runs any teardown, so the proxy learns that its handle
    is gone only when the call fails. What it must not do is hang, and what it
    must not do is return a value.
    """
    async with (
        shared_nix_environment.rpc_session() as session,
        session.store() as store,
        session.eval(store) as evaluator,
    ):
        root = await evaluator.string("{ a = 1; }")
        assert await root.attr("a").as_int() == 1

        proxy = _eval_proxy(evaluator)
        worker: Any = proxy._worker  # type: ignore[reportPrivateUsage] -- killing the worker is the point
        worker._worker_proc.kill()  # type: ignore[reportPrivateUsage] -- as above

        with pytest.raises(Exception) as excinfo:  # noqa: PT011 -- the class is what this test measures
            await root.attr("a").as_int()

        note(after_worker_death=f"{type(excinfo.value).__name__}: {str(excinfo.value)[:120]}")
