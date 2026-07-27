"""The test harness's own teeth: is beartype actually checking what we think?

Every other test in this suite asserts something about nanopynix. These assert
something about the instrumentation those tests run under, because it is the
kind of thing that fails *silently* -- an uninstrumented process runs every
test to a clean pass, it just stops catching anything. Nothing else here would
notice.

The subprocess half is not hypothetical. When beartype was first wired up, the
hook was installed only in the pytest process while ``NANOPYNIX_BEARTYPING``
was exported to the whole environment. Children therefore got the flag --
promoting every ``if TYPE_CHECKING or BEARTYPING:`` import to a real one --
without the import hook that is the flag's entire reason for existing. The
worker side of the rpc engine ran unchecked while inproc's equivalent was
fully checked, and the suite was green throughout.
"""

from __future__ import annotations

import multiprocessing
import os
import re
import sys

import beartype.roar
import pytest

from nanopynix._typechecking import BEARTYPING, no_runtime_type_check
from nanopynix.settings import normalize_nix_path, normalize_nix_settings
from tests.support.subprocess_output import run_process

# An `int` is wrong for both functions probed below -- `normalize_nix_path`
# takes `str | Sequence[str] | None`, `normalize_nix_settings` takes
# `NixSettings | os.PathLike[str] | str | None`. Both are cheap and pure, so
# probing them costs nothing and touches no Nix state.
_PROBE_ARGUMENT = 123


def _is_instrumented() -> bool:
    """Did beartype's hook reach the module ``normalize_nix_path`` lives in?"""
    try:
        normalize_nix_path(_PROBE_ARGUMENT)  # type: ignore[arg-type] -- deliberately wrong, that is the probe
    except beartype.roar.BeartypeCallHintParamViolation:
        return True
    else:
        return False


def _child_report() -> tuple[bool, bool]:
    """Run in a forkserver child: ``(flag_set, actually_instrumented)``.

    Module-level so it is picklable. The mismatch this exists to catch is the
    two disagreeing.
    """
    from nanopynix._typechecking import BEARTYPING as child_flag

    return child_flag, _is_instrumented()


def test_the_pytest_process_is_instrumented() -> None:
    """Baseline: without this, every other assertion here proves nothing."""
    assert BEARTYPING is True, "NANOPYNIX_BEARTYPING was not set before nanopynix was imported"
    assert _is_instrumented(), "beartype's import hook did not reach nanopynix in the pytest process"


def test_subprocesses_inherit_the_startup_shim() -> None:
    """``PYTHONPATH`` carries the shim directory, which is how children get it.

    A freshly exec'd interpreter cannot inherit an import hook; the only thing
    that crosses the boundary is the environment. This asserts the mechanism
    is armed, and the test below asserts it actually fires.
    """
    entries = os.environ.get("PYTHONPATH", "").split(os.pathsep)
    assert any(entry.endswith("_subprocess_startup") for entry in entries), entries


@pytest.mark.skipif(
    "forkserver" not in multiprocessing.get_all_start_methods(),
    reason="the Nix worker is forked from a forkserver helper; nothing to check without one",
)
def test_a_forkserver_child_is_instrumented_not_merely_flagged() -> None:
    """The regression this file was written for.

    ``preload`` mirrors what ``rpc/client/_pool.py`` passes when it spawns a
    worker, so the child imports the same module graph a real worker does --
    under the same conditions, at the same point in startup.

    Both halves are asserted because the failure mode was *disagreement*: the
    flag alone was true, which is what made the gap invisible. A child with
    neither would be a different (and louder) bug.
    """
    ctx = multiprocessing.get_context("forkserver")
    ctx.set_forkserver_preload(["nanopynix.rpc.worker._worker"])
    with ctx.Pool(processes=1) as pool:
        flag_set, instrumented = pool.apply(_child_report)

    assert flag_set is True, "the child did not inherit NANOPYNIX_BEARTYPING"
    assert instrumented, (
        "the child inherited NANOPYNIX_BEARTYPING but no import hook: its "
        "`if TYPE_CHECKING or BEARTYPING:` imports were promoted while nothing "
        "checked the hints they exist to resolve"
    )


class TestTheExemptionDecoratorDoesWhatItClaims:
    """``no_runtime_type_check`` trades runtime checking for nothing else.

    It exists because ``typing.no_type_check`` achieves the same runtime
    exemption at the cost of static checking -- pyright erases the signature
    and stops analysing the body. That half cannot be asserted from inside a
    test process; what can be asserted is that the runtime half still works,
    including for hints that fail when beartype *decorates* rather than when
    the function is called.
    """

    def test_an_exempt_function_raises_its_own_error_not_beartypes(self) -> None:
        """Exercised through a real exempt site, not a locally defined one.

        A function defined *here* would prove nothing: `tests` is deliberately
        absent from the instrumented package list, so beartype never decorates
        it and the assertion holds with or without the decorator.
        `normalize_nix_settings` is one of the 19 real exemptions, and it is
        the clearest of them -- it validates its own argument and documents a
        `TypeError`, which beartype's check would preempt with a
        `BeartypeCallHintParamViolation` (a sibling of `TypeError`, not a
        subclass). So the exception *type* is what discriminates: delete the
        decorator from `nanopynix/settings.py` and this fails.
        """
        with pytest.raises(TypeError, match="settings must be") as raised:
            normalize_nix_settings(_PROBE_ARGUMENT)  # type: ignore[arg-type] -- deliberately wrong, that is the probe

        assert not isinstance(raised.value, beartype.roar.BeartypeCallHintParamViolation)

    def test_an_unexempted_function_still_is(self) -> None:
        """The control for the test above.

        Without it, that test passes just as happily on a process where
        beartype was never installed -- which is exactly how the subprocess
        gap stayed hidden for a whole commit. `tests` is deliberately not in
        the instrumented package list, so the control has to be a function
        from a package that is.
        """
        assert _is_instrumented()

    def test_it_sets_the_attribute_beartype_reads(self) -> None:
        """The mechanism, stated once so a future reader need not infer it.

        ``__no_type_check__`` is what ``typing.no_type_check`` sets and what
        beartype's decorator checks before it analyses any hint -- which is
        why this also covers hints that would fail at decoration time, such as
        ``Never`` or an unresolvable forward reference.
        """

        @no_runtime_type_check
        def anything() -> None: ...

        assert anything.__no_type_check__ is True  # type: ignore[attr-defined] -- set by the decorator under test


# Callables beartype cannot decorate at all, with why. Everything here is
# *silently unchecked* -- beartype gives up on the whole callable, not just the
# offending hint -- so an entry is a real hole and needs a real reason.
#
# Both of these annotate `grpclib._typing.IServable`, a third-party protocol
# that is not `@runtime_checkable`. beartype has to `isinstance` against a hint
# to check it, `typing` refuses that for a bare protocol, and beartype's only
# recourse is to skip. Not fixable here: the decorator would have to go on
# grpclib's declaration. nanopynix's own protocols were in this list until
# `@runtime_checkable` was added to them; see nanopynix/protocols.py.
UNDECORATABLE: dict[str, str] = {
    "nanopynix.rpc.worker._worker.worker_service_factory": "returns grpclib's non-runtime-checkable IServable",
    "nanopynix.rpc.worker._worker._shutdown_worker": "takes grpclib's non-runtime-checkable IServable",
}

# beartype names the callable in the warning's first line, in one of several
# phrasings ("Function ...()", "Coroutine factory function ...()").
_UNDECORATABLE_PATTERN = re.compile(r"BeartypeClawDecorWarning: .*? ([\w.]+)\(\) in file")

# Imports everything instrumented, then probes whether the hook actually fired.
# The probe matters for the day UNDECORATABLE is empty: without it, a child
# where beartype never installed would emit no warnings and pass vacuously.
_INSTRUMENTED_IMPORT_PROBE = """
import beartype.roar
import nanopynix, nanopynix_helpers, pynix, ekn
from nanopynix.settings import normalize_nix_path

try:
    normalize_nix_path(123)
except beartype.roar.BeartypeCallHintParamViolation:
    print("INSTRUMENTED")
"""


async def test_no_callable_is_silently_undecoratable_beyond_the_known_list() -> None:
    """The regression test for making ``nanopynix.protocols`` runtime-checkable.

    That change exists so beartype stops skipping functions annotated with
    those protocols. Asserting the decorator is present (over in
    ``test_protocols.py``) pins the cause; this pins the *effect*, which is
    what actually matters and which nothing else observes -- a skipped
    callable produces a warning on stderr and a green test run.

    An allowlist rather than a count, so it also catches a skip arising from
    some other cause: a stub-only annotation beartype cannot import, say, or
    a hint that raises at decoration time. Any of those is a hole in the
    instrumentation and should have to be named here with a reason.

    A subprocess because the pytest process imported these packages before
    this test could watch it happen. The child inherits ``PYTHONPATH`` and
    ``NANOPYNIX_BEARTYPING`` from the hook, which is exactly how a real Nix
    worker gets instrumented.
    """
    result = await run_process([sys.executable, "-c", _INSTRUMENTED_IMPORT_PROBE])

    assert result.returncode == 0, result.describe()
    assert "INSTRUMENTED" in result.stdout, f"the child ran without beartype's hook; {result.describe()}"

    found = set(_UNDECORATABLE_PATTERN.findall(result.stderr))
    unexplained = sorted(found - set(UNDECORATABLE))
    assert not unexplained, (
        "beartype silently skipped these callables entirely -- fix the annotation, or add each to "
        f"UNDECORATABLE with a reason: {unexplained}\n{result.stderr}"
    )

    stale = sorted(set(UNDECORATABLE) - found)
    assert not stale, f"UNDECORATABLE lists callables beartype now decorates fine (or that no longer exist): {stale}"


def test_the_probe_would_notice_if_it_stopped_working() -> None:
    """Guards the helper every assertion above leans on.

    ``_is_instrumented`` reports False both when beartype is absent and when
    ``normalize_nix_path`` stops rejecting the probe argument (a signature
    change, say). Pinning the second here means a silent widening shows up as
    this test failing rather than as instrumentation appearing to vanish.
    """
    assert sys.modules.get("nanopynix.settings") is not None
    with pytest.raises(beartype.roar.BeartypeCallHintParamViolation):
        normalize_nix_path(_PROBE_ARGUMENT)  # type: ignore[arg-type] -- deliberately wrong, that is the probe
