"""The command line of a stdio worker, written and read in one place.

A worker that starts by ``exec`` carries nothing from the process that started
it except its environment and its arguments. The forkserver and ``spawn``
carry a pickled ``functools.partial`` instead, which is how
:class:`~nanopynix.OverlayNamespace` reaches ``worker_service_factory`` on
those two start methods. An ``exec`` cannot, so the namespace travels here.

**The client and the worker both import this module**, so the contract has one
definition. ``rpc/client/_pool.py`` calls :func:`worker_argv`, and
``rpc/worker/__main__.py`` calls :func:`parse_worker_argv`.

The module the argument vector names is the package, and not
``nanopynix.rpc.worker._worker``. Measured: ``python -m
nanopynix.rpc.worker._worker`` prints ``RuntimeWarning: ... found in
sys.modules after import of package ... but prior to execution`` on the stderr
the client inherits, and runs the module twice -- because ``nanopynix``
imports ``rpc``, which imports ``client/_pool.py``, which imports ``_worker``.
``nanopynix/rpc/worker/__main__.py`` has no such cycle.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from typing import TYPE_CHECKING

from pydantic import TypeAdapter, ValidationError

from nanopynix._typechecking import BEARTYPING
from nanopynix.namespace import OverlayNamespace

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Sequence

WORKER_MODULE = "nanopynix.rpc.worker"
"""What ``python -m`` runs. See the module docstring for why it is the package."""

NAMESPACE_OPTION = "--namespace"

NAMESPACE_JSON = TypeAdapter(OverlayNamespace)
"""Both directions of the namespace, as JSON.

pydantic validates a stdlib dataclass directly, so ``OverlayNamespace`` stays
what it is and gains no base class. What it does need is
``__pydantic_config__``: the default configuration accepts an unknown key and
discards it, which would hand the worker a namespace that is not the one the
client asked for. That attribute, and the measurement behind it, are on the
class.
"""


@dataclasses.dataclass(frozen=True)
class WorkerArguments:
    """What a worker process was told on its command line."""

    namespace: OverlayNamespace | None = None


def worker_argv(*, namespace: OverlayNamespace | None = None) -> list[str]:
    """Build the argument vector that starts one stdio worker.

    ``sys.executable``, so that the worker runs the interpreter of the client
    and therefore the same installed package. That is also why
    ``InitRequest`` carries no version field: the two ends cannot be different
    builds. A peer reached over ``connect_ssh_stdio`` can be, and that peer is
    not this client.
    """
    argv = [sys.executable, "-m", WORKER_MODULE]
    if namespace is not None:
        argv += [NAMESPACE_OPTION, NAMESPACE_JSON.dump_json(namespace).decode()]
    return argv


def parse_worker_argv(argv: Sequence[str] | None = None) -> WorkerArguments:
    """Read the arguments of this worker process.

    ``None`` reads ``sys.argv[1:]``, which is what the console script and
    ``python -m`` both want.

    A malformed value ends the process with the usage message and status 2,
    the way argparse ends any command. The client never sends one -- it calls
    :func:`worker_argv` -- so the reader of that message is a person who ran
    ``nanopynix-worker`` by hand, and pydantic names the field and the reason.
    """
    parser = argparse.ArgumentParser(
        prog="nanopynix-worker",
        description="Serve the nanopynix worker over this process's stdin and stdout.",
    )
    parser.add_argument(
        NAMESPACE_OPTION,
        type=_namespace_argument,
        default=None,
        metavar="JSON",
        help=(
            "Enter a user namespace and mount an overlay store in it, before Nix loads. "
            "The value is one OverlayNamespace as a JSON object."
        ),
    )
    parsed = parser.parse_args(None if argv is None else list(argv))
    return WorkerArguments(namespace=parsed.namespace)


def _namespace_argument(value: str) -> OverlayNamespace:
    """Validate one ``--namespace`` value, for argparse.

    argparse turns a ``ValueError`` from a ``type`` callable into "invalid
    _namespace_argument value", which throws away everything pydantic said. It
    prints an ``ArgumentTypeError`` as it stands.
    """
    try:
        return NAMESPACE_JSON.validate_json(value)
    except ValidationError as exc:
        raise argparse.ArgumentTypeError(f"{NAMESPACE_OPTION}: {exc}") from exc
