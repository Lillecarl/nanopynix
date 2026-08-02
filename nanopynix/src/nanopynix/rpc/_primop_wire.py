"""How a manager-side primop's failure crosses to the worker.

A primop declared ``rpc=True`` runs on the **client**, and the Nix evaluator
that calls it runs in the **worker**. So the caller's own function raises in
one process, and Nix has to be told about it in another.

**The backchannel carries a string and nothing else.** Its response frame has
one ``error: str | None`` field, and the transport fills it with ``str(exc)``
of whatever the handler raised. There is no status code on this path and no
details field, so the ``GRPCError`` the manager handler used to raise was
worse than useless: its status was discarded and its ``repr`` became the
message. A caller saw

    error: RemoteCallError: (<Status.INTERNAL: 13>, 'no such user', None)

for a primop that raised ``PrimopError("no such user")``.

Because one string is all there is, this module puts the whole answer in that
string. :func:`encode` renders the text Nix should show, using the same rule
the C++ bridge applies to a primop that raises in the worker
(``nix_expr.cpp``): a ``PrimopError`` or a ``ValueError`` is a deliberate
rejection and its message is shown bare, and any other class is unexpected and
keeps its name as a prefix. The worker then re-raises a ``PrimopError`` holding
that exact text, so Nix renders a manager-side primop failure and a
worker-side one identically.

The marker exists to keep a transport failure distinguishable. Both arrive at
the worker as the same ``RemoteCallError``, and dressing a dead backchannel up
as the caller's exception would be a lie. Only a marked string is the caller's.
"""

from __future__ import annotations

from nanopynix_bindings.expr import PrimopError

# Prefixed onto the wire string so the worker can tell "the caller's primop
# raised" from "the backchannel broke". A message that begins with this by
# accident is not a real risk, and misreading one would only mean a genuine
# primop failure renders as a transport failure -- the safe direction.
MARKER = "\x00nanopynix.primop-raised\x00"


def encode(exc: BaseException) -> str:
    """Render ``exc`` as the text Nix should show, marked as a primop failure.

    Mirrors ``py_primop_bridge`` in ``nix_expr.cpp``. Keep the two in step: a
    class that is deliberate there and unexpected here would make the same
    primop read differently depending on where it ran.
    """
    detail = str(exc)
    if not isinstance(exc, PrimopError | ValueError):
        detail = f"{type(exc).__name__}: {detail}"
    return MARKER + detail


def decode(message: str) -> str | None:
    """The text a marked ``message`` carries, or ``None`` when it is unmarked."""
    if not message.startswith(MARKER):
        return None
    return message[len(MARKER) :]


def reraise_if_primop_failure(exc: BaseException) -> None:
    """Re-raise a marked backchannel failure as the caller's primop failure.

    Returns without doing anything when ``exc`` is not one, which leaves a
    transport failure to propagate as itself.
    """
    text = decode(str(exc))
    if text is None:
        return
    raise PrimopError(text) from exc
