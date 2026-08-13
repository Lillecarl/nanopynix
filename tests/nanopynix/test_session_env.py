"""``Session(env=...)``: what reaches Nix, and what a session refuses.

A session carries ``env`` to the process where Nix runs. It serves every name
Nix reads after ``initLibStore`` -- ``NIX_SSHOPTS`` above all, which is the one
way to reach a host through a bastion, because ``ProxyCommand`` and
``ProxyJump`` have no store-URI equivalent.

**The refusals are the other half, and they are why this module is here rather
than beside one engine.** A name Nix reads while ``libnixstore`` loads cannot
take effect in either engine, and a name a session parameter already owns would
contradict that parameter. Both are refused, with the route that works. Both
engines must refuse exactly the same set: a caller that moved from one engine
to the other because of the environment would be the bug this feature is
supposed to remove.

The passthrough is asserted here for rpc only. inproc runs Nix in this process,
so ``env`` there is an assignment to ``os.environ`` of the pytest process, and
``tests/nanopynix/inproc/test_inproc_process_env.py`` asserts it in a
subprocess -- for the reason that module's docstring gives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import nanopynix.inproc
import nanopynix.rpc
from nanopynix._env import OWNED_BY_NANOPYNIX, READ_WHILE_LIBSTORE_LOADS

if TYPE_CHECKING:
    from nanopynix_testing.nix_environment import RpcSessionFactory

#: A name no Nix and no test fixture reads, so its only source is this module.
PROBE = "NANOPYNIX_TEST_ENV_PROBE"

_ENGINES = pytest.mark.parametrize(
    "session_class",
    [nanopynix.inproc.Session, nanopynix.rpc.Session],
    ids=["inproc", "rpc"],
)


# ── The refusals, which both engines share ───────────────────────────


@_ENGINES
@pytest.mark.parametrize("name", sorted(OWNED_BY_NANOPYNIX))
def test_a_name_a_session_parameter_owns_is_refused(session_class: Any, name: str) -> None:
    """A caller must not spell a session parameter twice, in two languages."""
    with pytest.raises(ValueError, match=name) as caught:
        session_class(env={name: "a value"})

    message = str(caught.value)
    assert name in message
    assert OWNED_BY_NANOPYNIX[name] in message, "the refusal must name the parameter that owns the name"


@_ENGINES
@pytest.mark.parametrize("name", sorted(READ_WHILE_LIBSTORE_LOADS))
def test_a_name_nix_reads_while_libstore_loads_is_refused(session_class: Any, name: str) -> None:
    """Silence is the failure this refusal replaces.

    Nix has already read each of these by the time a session exists, on at
    least one supported version, so the assignment would do nothing and report
    nothing.
    """
    with pytest.raises(ValueError, match=name) as caught:
        session_class(env={name: "a value"})

    message = str(caught.value)
    assert name in message
    assert READ_WHILE_LIBSTORE_LOADS[name] in message, "the refusal must name the route that works"


def test_no_name_is_in_both_refusal_sets() -> None:
    """One name, one reason. Two would make the message a coin toss."""
    assert not (set(OWNED_BY_NANOPYNIX) & set(READ_WHILE_LIBSTORE_LOADS))


@_ENGINES
@pytest.mark.parametrize(
    ("env", "fragment"),
    [
        pytest.param({"": "value"}, "empty name", id="empty-name"),
        pytest.param({"HAS=EQUALS": "value"}, "'=' or a NUL", id="equals-in-name"),
        pytest.param({"HAS\0NUL": "value"}, "'=' or a NUL", id="nul-in-name"),
        pytest.param({"NAME": "has\0nul"}, "holds a NUL", id="nul-in-value"),
    ],
)
def test_a_name_or_value_that_cannot_be_an_environment_variable_is_refused(
    session_class: Any, env: dict[str, str], fragment: str
) -> None:
    """``putenv`` takes ``name=value``, so both characters change what is set."""
    with pytest.raises(ValueError, match=fragment):
        session_class(env=env)


@_ENGINES
@pytest.mark.parametrize(
    "env",
    [
        pytest.param("NAME=value", id="a-string"),
        pytest.param({"NAME": 1}, id="a-non-string-value"),
        pytest.param({1: "value"}, id="a-non-string-name"),
    ],
)
def test_an_env_that_is_not_a_mapping_of_str_to_str_is_refused(session_class: Any, env: Any) -> None:
    """The constructor turns off beartype for its own guards, so this is ours."""
    with pytest.raises(TypeError, match="mapping of str to str"):
        session_class(env=env)


# ── The passthrough ──────────────────────────────────────────────────


async def _worker_reads(session: Any, name: str) -> str:
    """What ``builtins.getEnv`` sees, which is the environment Nix itself has."""
    async with session, session.store() as store, session.eval(store) as evaluator:
        value = await evaluator.string(f'builtins.getEnv "{name}"')
        return await value.as_string()


async def test_the_worker_reads_the_value_the_session_named(rpc_session: RpcSessionFactory) -> None:
    """The whole point: a variable set in this process would not arrive.

    The forkserver copies its environment once, when it starts, and reuses that
    copy for every worker after it. So ``os.environ[...] = ...`` here is not a
    workaround, and ``env`` is not a convenience for one.
    """
    assert await _worker_reads(rpc_session(env={PROBE: "from the session"}), PROBE) == "from the session"


async def test_the_worker_keeps_the_environment_it_inherited(rpc_session: RpcSessionFactory) -> None:
    """A merge, not a replacement.

    ``PATH`` rather than a variable this test sets: a variable set here would
    not reach the worker at all, which is what the test above measures, so it
    could not tell a merge from a replacement.
    """
    assert await _worker_reads(rpc_session(env={PROBE: "irrelevant"}), "PATH") != ""
