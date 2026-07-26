"""Typed Nix exceptions — the one hierarchy *both* engines raise.

``inproc`` and ``rpc`` must raise the same exception type for the same
failure, so ``except nanopynix.NixError`` works regardless of engine. Three
different amounts of information are available depending on where the error
crosses into Python, and this module has one entry point per case:

**A. Nix C++ → Python (nanobind).** The concrete C++ exception type survives
as the bound class's name, so ``exception_for_nix_type`` maps it directly.
:mod:`nanopynix.inproc` translates at its single call chokepoint.

**B. worker → client (gRPC).** The worker prefixes the type name onto the
status message, so the client recovers it the same way rather than guessing.

**C. Nix daemon → client (daemon protocol).** **Not ours.** The daemon
protocol structurally loses detail — upstream downgrades ``HashMismatch`` to
``OutputRejected`` on write (``common-protocol.cc:153-158``) and formats
fixed-output hashes into prose only. When no type name is available this is
the case, and message-pattern matching (``_CLASSIFIERS`` below) is the only
option left. That is why the classifier still exists, and the only thing it
is still for.

Build failures are separate from all three: they are not exceptions on the
wire at all. Nix reports them as a ``BuildResult`` whose ``status`` field
carries the failure *kind*, so :func:`build_error_from_result` turns that
structured status into a typed exception. Both engines use it.

Usage::

    try:
        await nix.store.query_path_info("/bad/path")
    except nanopynix.StoreError as e:
        print(e.error_type, e.msg)
    except nanopynix.HashMismatchError as e:
        print(e.status, e.drv_path)  # fixed-output hash mismatch, local store
"""

from __future__ import annotations

import re
from typing import Any, cast

from nanopynix_proto.nix.common import NixType
from strip_ansi import strip_ansi  # type: ignore[reportMissingTypeStubs] -- strip_ansi has no PEP 561 stubs

# ════════════════════════════════════════════════════════════════════
# Exception hierarchy
# ════════════════════════════════════════════════════════════════════


def _quoted_between(text: str, prefix: str, suffix: str) -> str | None:
    """Pull the quoted name out of a Nix message, or ``None``.

    Nix colourises its messages, so the quotes are usually separated from the
    name by ANSI escapes; strip them before matching.
    """
    plain = strip_ansi(text)
    start = plain.find(prefix)
    if start < 0:
        return None
    start += len(prefix)
    end = plain.find(suffix, start)
    return plain[start:end] if end >= 0 else None


class NixError(RuntimeError):
    """Base for all Nix-originated errors from RPC calls.

    Attributes:
        error_type: the classified Nix error kind (e.g. ``"TypeError"``).
        msg: the error message string from Nix.
        raw: Nix's own rendering of the error -- ``showErrorInfo`` on the
            underlying ``nix::ErrorInfo``, which is what the Nix CLI prints.
            Produced on both engines; on rpc it rides the status trailer.
        info: ErrorInfo dict (traces, suggestions, level, etc.) or None.
    """

    def __init__(self, error_type: str, msg: str, *, raw: str = "", info: dict[str, Any] | None = None) -> None:
        self.error_type = error_type
        self.msg = msg
        self.raw = raw
        self.info = info
        super().__init__(f"[{error_type}] {msg}")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(error_type={self.error_type!r}, msg={self.msg!r})"

    @property
    def msg_without_ansi(self) -> str:
        """Error message with ANSI color escapes removed, preserving other text."""

        return strip_ansi(self.msg)

    @property
    def raw_without_ansi(self) -> str:
        """:attr:`raw` with ANSI color escapes removed."""

        return strip_ansi(self.raw)


class StoreError(NixError):
    """Errors from Nix store operations (invalid paths, hash mismatches, etc.)."""


class EvalError(NixError):
    """Errors from Nix expression evaluation."""


class HashMismatchError(NixError):
    """A declared fixed-output hash did not match what Nix actually got.

    Deliberately **cross-cutting**: the same failure reaches Python from two
    unrelated places, and callers care that it is a hash mismatch far more than
    they care which. ``builtins.fetchurl`` with a wrong ``sha256`` fails during
    *evaluation* (:class:`EvalHashMismatchError`, also an :class:`EvalError`),
    while a fixed-output derivation fails during the *build*
    (:class:`BuildHashMismatchError`, also a :class:`BuildError`). Catching
    ``HashMismatchError`` gets both; catching ``EvalError`` or ``StoreError``
    still gets the respective one.

    Raised bare only at boundary C, where the message says "hash mismatch" but
    the daemon protocol has already discarded which kind it was.
    """


class NixTypeError(EvalError):
    """Type errors during evaluation (expected X but found Y)."""


class NixAssertionError(EvalError):
    """Assertion failures in Nix expressions."""


class UndefinedVarError(EvalError):
    """Undefined variable in Nix expression."""


class ThrownError(EvalError):
    """Nix ``throw`` or ``builtins.throw`` called."""


class InfiniteRecursionError(EvalError):
    """Infinite recursion in Nix evaluation."""


class RestrictedPathError(EvalError):
    """Access to a restricted path in pure evaluation mode."""


class MissingArgumentError(EvalError):
    """Missing function argument in Nix expression."""


class MissingAttributeError(EvalError, KeyError):
    """An attribute is absent from a Nix attrset (``value.attr("nope")``).

    Both a Nix error and a Python one, deliberately, and paired with
    :class:`ListIndexError` -- the two are the attrset and list halves of the
    same "you asked for something that is not there" question, so they must
    answer it the same way. The alternative was to make both plain
    :class:`EvalError`; that was rejected because Python's exceptions can carry
    everything Nix has here, so there is nothing to trade away:

    * ``except KeyError`` works on a Nix attrset exactly as on a ``dict``, and
      ``except NixError`` still catches it. The MRO satisfies both.
    * :attr:`key` names the attribute, so callers branch on a value rather than
      parsing prose.
    * ``info["suggestions"]`` carries Nix's own "Did you mean ...?" ranking,
      computed from the attrset's symbol table by the same
      ``Suggestions::bestMatches`` Nix uses for ``{ foo = 1; }.fooo``. This is
      the part that would have been lost by raising a bare ``KeyError`` from
      Python: the candidate names exist only on the C++ side.

    ``EvalHashMismatchError`` above is the same pattern -- one failure that
    genuinely belongs to two hierarchies.
    """

    def __init__(self, error_type: str, msg: str, *, raw: str = "", info: dict[str, Any] | None = None) -> None:
        super().__init__(error_type, msg, raw=raw, info=info)
        self.key = _quoted_between(msg, "attribute '", "'")

    def __str__(self) -> str:
        # Without this, the MRO reaches KeyError.__str__ first, which renders
        # `repr(args[0])` -- so every message would arrive wrapped in quotes.
        # That is a dict-lookup convention, not ours.
        return f"[{self.error_type}] {self.msg}"

    @property
    def suggestions(self) -> list[str]:
        """Nix's "Did you mean ...?" candidates, closest first; ``[]`` if none."""
        info = self.info or {}
        raw: object = info.get("suggestions")
        if not isinstance(raw, list):
            return []
        return [str(item) for item in cast("list[object]", raw)]


class ListIndexError(EvalError, IndexError):
    """A list index is out of bounds (``value.list_get(99)``).

    The list half of the pair described on :class:`MissingAttributeError`; see
    there for why these are Python exceptions as well as Nix ones. Nix has no
    equivalent of ``suggestions`` for an index, so the extra information here
    is just :attr:`index`.
    """

    def __init__(self, error_type: str, msg: str, *, raw: str = "", info: dict[str, Any] | None = None) -> None:
        super().__init__(error_type, msg, raw=raw, info=info)
        match = re.search(r"index (\d+)", strip_ansi(msg))
        self.index: int | None = int(match.group(1)) if match else None

    def __str__(self) -> str:
        # IndexError does not repr its argument the way KeyError does, so this
        # is only here to keep the two halves of the pair rendering alike.
        return f"[{self.error_type}] {self.msg}"


class EvalHashMismatchError(EvalError, HashMismatchError):
    """``builtins.fetchurl``/``fetchTarball`` got a different hash than declared.

    This fails during evaluation, so Nix reports it as a plain
    ``nix::EvalError``; the hash mismatch is only visible in the message. It is
    still a :class:`HashMismatchError`, so fixed-output update logic can treat
    it uniformly with the build-time variant.
    """


class ParseError(NixError):
    """Nix expression parse error."""


class UsageError(NixError):
    """Invalid usage or configuration."""


class UnimplementedError(NixError):
    """Nix reported an unimplemented operation (``nix::UnimplementedError``)."""


class NixSysError(NixError):
    """An OS-level failure reported by Nix (``nix::SysError``)."""


# ── Store errors with a bound C++ counterpart ────────────────────────


class InvalidPathError(StoreError):
    """A store path is not valid in this store (``nix::InvalidPath``)."""


class UnsupportedError(StoreError):
    """The store does not support this operation (``nix::Unsupported``)."""


class BadStorePathError(StoreError):
    """A string is not a well-formed store path (``nix::BadStorePath``)."""


# ── Build failures ───────────────────────────────────────────────────
#
# Build failures are not exceptions on the wire: Nix returns a BuildResult
# whose `status` names the failure kind. `build_error_from_result` maps that
# status onto these classes, so callers can branch on the kind instead of
# matching on prose. Both engines raise these.


class BuildError(StoreError):
    """A derivation failed to build.

    Attributes:
        status: Nix's own failure-status string (e.g. ``"hash-mismatch"``),
            verbatim from ``BuildResult.status``. Authoritative; prefer it over
            parsing :attr:`msg`.
        drv_path: the derivation that failed, when Nix reported one.
    """

    def __init__(  # noqa: PLR0913 -- mirrors NixError.__init__ plus the two build-specific fields; all keyword-only
        self,
        error_type: str,
        msg: str,
        *,
        status: str = "",
        drv_path: str = "",
        raw: str = "",
        info: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.drv_path = drv_path
        super().__init__(error_type, msg, raw=raw, info=info)


class PermanentBuildError(BuildError):
    """The build failed and retrying it unchanged will not help."""


class TransientBuildError(BuildError):
    """The build failed for a reason that may not recur; retrying may succeed."""


class InputRejectedError(BuildError):
    """Nix rejected one of the derivation's inputs."""


class OutputRejectedError(BuildError):
    """Nix rejected the derivation's output.

    Note this is also what a fixed-output hash mismatch degrades to when it
    crosses the *daemon* protocol, which cannot represent ``HashMismatch``
    (see this module's docstring, boundary C). A local store reports
    :class:`HashMismatchError` instead.
    """


class BuildHashMismatchError(BuildError, HashMismatchError):
    """A fixed-output *derivation* produced a different hash than declared.

    Only reachable when the failure did not have to cross the daemon protocol
    (see :class:`OutputRejectedError`). When this *is* raised, ``msg`` contains
    Nix's ``specified:``/``got:`` prose -- the hashes have no structured
    representation anywhere in Nix, so parsing the message is the only way to
    recover them (``nanopynix_helpers.fod`` does exactly that).
    """


class CachedBuildFailureError(BuildError):
    """Nix replayed a previously cached failure instead of rebuilding."""


class BuildTimedOutError(BuildError):
    """The build exceeded its configured timeout."""


class MiscBuildError(BuildError):
    """The build failed for a reason Nix did not classify further."""


class DependencyFailedError(BuildError):
    """A dependency of the requested derivation failed, not the target itself."""


class LogLimitExceededError(BuildError):
    """The build produced more log output than the configured limit allows."""


class NotDeterministicError(BuildError):
    """A check build produced different output than the original build."""


class NoSubstitutersError(BuildError):
    """No substituter could supply the path and it could not be built."""


class ObjectMisuseError(RuntimeError):
    """Base for misusing a nanopynix object, as opposed to an error Nix raised.

    Named for the object rather than for a transport: this was
    ``EvalProxyError``, "master-side eval proxy misuse", which described only
    rpc and only the evaluator. The same misuses exist on inproc, where there
    is no proxy and no master, and on stores, which are not evaluator objects
    at all. Every situation below is one a caller reached without Nix being
    consulted, so :class:`NixError` is not the right family for any of them.
    """


class ObjectLifetimeError(ObjectMisuseError):
    """A nanopynix object was used after it was closed or released.

    The one ``except`` clause for "the thing I am holding is gone", whichever
    kind of thing it is and whichever engine produced it. Its subclasses split
    by *what* died, not by transport: both engines raise the same class for
    the same situation.

    Worth a base of its own rather than leaving callers to catch
    :class:`ObjectMisuseError`, which also covers a value of the wrong Nix
    type -- a live object answering a question it cannot answer, which is not
    the same problem and rarely wants the same handling.
    """


class SessionClosedError(ObjectLifetimeError):
    """A session was used after it closed, or before it was opened."""


class StoreClosedError(ObjectLifetimeError):
    """A store was used after it closed, or before it was opened."""


class EvalSessionClosedError(ObjectLifetimeError):
    """An evaluator, or a value belonging to one, was used after it closed."""


class ValueReleasedError(ObjectLifetimeError):
    """A value was used after its explicit release."""


class LockedFlakeReleasedError(ObjectLifetimeError):
    """A locked flake was used after its explicit release.

    Distinct from :class:`ValueReleasedError`, which rpc used to raise here: a
    locked flake is not a Nix value, and a caller releasing values in a loop
    should not have that ``except`` silently swallow a flake handle it never
    meant to touch. inproc drew the distinction first.
    """


class UnresolvedValueError(ObjectMisuseError):
    """A lazy child value was inspected before it had a handle."""


class WrongNixTypeError(ObjectMisuseError, TypeError):
    """A Nix value had a different type than the operation requires."""

    def __init__(self, *, expected: NixType | str, actual: NixType | str) -> None:
        self.expected = expected.name.lower() if isinstance(expected, NixType) else expected
        self.actual = actual.name.lower() if isinstance(actual, NixType) else actual
        super().__init__(f"Nix value is {self.actual}, expected {self.expected}")


class ForeignValueError(ObjectMisuseError, ValueError):
    """A value from another eval session was used where a local value is required."""


# ════════════════════════════════════════════════════════════════════
# Classification — string-based, matches Nix error message patterns
# ════════════════════════════════════════════════════════════════════

# Tuples of (regex, Python class, error_type_name).
# Ordered: more specific patterns first.
_CLASSIFIERS: list[tuple[re.Pattern[str], type[NixError], str]] = [
    # ── Eval errors ──────────────────────────────────────────────
    (re.compile(r"undefined variable"), UndefinedVarError, "UndefinedVarError"),
    (re.compile(r"infinite recursion"), InfiniteRecursionError, "InfiniteRecursionError"),
    (re.compile(r"assertion .+ failed"), NixAssertionError, "AssertionError"),
    (re.compile(r"access to (absolute path|URI).+forbidden"), RestrictedPathError, "RestrictedPathError"),
    (re.compile(r"threw.*(?:while|error)"), ThrownError, "ThrownError"),
    (re.compile(r"function .+ called without required argument"), MissingArgumentError, "MissingArgumentError"),
    (re.compile(r"invalid value.*expected"), UsageError, "UsageError"),
    (re.compile(r"expected (?:a |an )"), NixTypeError, "TypeError"),
    (re.compile(r"cannot coerce .+ to a"), NixTypeError, "TypeError"),
    (re.compile(r"cannot add .+ to"), NixTypeError, "TypeError"),
    (re.compile(r"cannot compare"), NixTypeError, "TypeError"),
    (re.compile(r"but found"), NixTypeError, "TypeError"),
    (re.compile(r"builtin .+ not found"), EvalError, "EvalError"),
    (re.compile(r"attribute .+ missing"), EvalError, "EvalError"),
    (re.compile(r"integer overflow"), EvalError, "EvalError"),
    (re.compile(r"doesn't represent an absolute path"), EvalError, "EvalError"),
    # ── Parse errors ─────────────────────────────────────────────
    (re.compile(r"syntax error|parse error|unexpected (?:token|EOF)"), ParseError, "ParseError"),
    # ── Store errors ─────────────────────────────────────────────
    (re.compile(r"path .+ is not valid"), StoreError, "StoreError"),
    (re.compile(r"path .+ is not in the Nix store"), StoreError, "StoreError"),
    # Hash mismatches, most specific first. The eval-time variant must precede
    # the neutral one so that refining within a known EvalError can reach it --
    # a bare HashMismatchError is not an EvalError and would be rejected.
    (re.compile(r"hash mismatch in file downloaded"), EvalHashMismatchError, "HashMismatchError"),
    (re.compile(r"hash mismatch in fixed-output derivation"), BuildHashMismatchError, "HashMismatchError"),
    (re.compile(r"hash mismatch"), HashMismatchError, "HashMismatchError"),
    (re.compile(r"does not exist"), StoreError, "StoreError"),
    (re.compile(r"lacks a signature"), StoreError, "StoreError"),
    (re.compile(r"is not a valid derivation"), StoreError, "StoreError"),
    # ── Usage errors ─────────────────────────────────────────────
    (re.compile(r"setting .+ is a path and paths cannot be empty"), UsageError, "UsageError"),
    # ── System errors ────────────────────────────────────────────
    (re.compile(r"error \(ignored\)"), NixError, "Error"),  # swallowed by ignoreException
]


# ════════════════════════════════════════════════════════════════════
# Boundaries A and B — the Nix C++ type name is known, so use it
# ════════════════════════════════════════════════════════════════════

# Nix C++ exception type name -> Python class. Keys are the names nanobind
# registers in `nanopynix-bindings` (`nb::exception<nix::EvalError>(m,
# "EvalError", ...)`), which are also the names the RPC worker puts on the
# wire as a `TypeName: message` prefix. Authoritative when present: the
# concrete C++ type is strictly better information than any message pattern.
_NIX_EXCEPTION_TYPES: dict[str, type[NixError]] = {
    # The catch-all. Nix throws plain nix::Error freely from libstore and
    # libexpr, and until nix_errors.cpp gave the hierarchy a single translator
    # there was nothing to bind it to, so all of those arrived as a bare
    # RuntimeError with the ErrorInfo discarded. `refine_within` can still
    # narrow it by message, which is what makes a coarse type usable.
    "Error": NixError,
    # nix_errors.cpp -- all of these now live in one module
    # EvalBaseError is Nix's parent of EvalError, and it is what reaches us for
    # StackOverflowError (max-call-depth), IFDError, and RecoverableEvalError,
    # which derive from it rather than from EvalError. It maps to the same
    # public class deliberately: Nix's reason for the split is whether the
    # error may be *cached*, which no Python caller can act on, so giving it a
    # distinct public type would add surface for a distinction nobody can use.
    "EvalBaseError": EvalError,
    "EvalError": EvalError,
    "ParseError": ParseError,
    "TypeError": NixTypeError,
    "UndefinedVarError": UndefinedVarError,
    "AssertionError": NixAssertionError,
    "ThrownError": ThrownError,
    "MissingAttributeError": MissingAttributeError,
    "ListIndexError": ListIndexError,
    "MissingArgumentError": MissingArgumentError,
    "RestrictedPathError": RestrictedPathError,
    "InfiniteRecursionError": InfiniteRecursionError,
    # store-side
    "InvalidPath": InvalidPathError,
    "Unsupported": UnsupportedError,
    "BadStorePath": BadStorePathError,
    "BuildError": BuildError,
    # util-side
    "SysError": NixSysError,
    "UsageError": UsageError,
    "UnimplementedError": UnimplementedError,
}


def exception_for_nix_type(type_name: str) -> type[NixError] | None:
    """Return the Python class for a Nix C++ exception type name, if known."""
    return _NIX_EXCEPTION_TYPES.get(type_name)


def refine_within(base: type[NixError], msg: str) -> tuple[type[NixError], str]:
    """Narrow an authoritative exception class using message patterns.

    The Nix C++ type is authoritative but sometimes *coarser* than the message:
    ``1 + "a"`` and an infinite recursion both arrive as plain
    ``nix::EvalError``, though Nix's own message says exactly which it is. So
    message matching is still useful -- but only to **narrow**, never to widen
    or contradict. A pattern-derived class is accepted only when it is a proper
    subclass of *base*; otherwise *base* stands.

    This is what keeps the classifier honest. Before, a pattern could override
    a known type and claim ``StoreError`` for what Nix called an
    ``InvalidPath``; now that answer is rejected because it is a supertype.
    """
    candidate, candidate_name = _classify(msg, "")
    if candidate is not base and issubclass(candidate, base):
        return candidate, candidate_name or candidate.__name__
    return base, base.__name__


def translate_nix_exception(exc: BaseException) -> NixError | None:
    """Map a raw nanobind Nix exception onto the public hierarchy (boundary A).

    Returns ``None`` when *exc* is not a Nix binding exception and should
    propagate untouched -- including when it is already a :class:`NixError`,
    so a translated exception passing back through a funnel is not rewrapped.

    The ``__module__`` check is load-bearing, not defensive: several bound
    classes share a name with a Python builtin (``TypeError``,
    ``AssertionError``), and matching on the name alone would silently convert
    an ordinary Python ``TypeError`` from caller code into a Nix eval error.

    ``raw``/``info`` carry over directly: the bindings attach them under those
    same names (``nanopynix-bindings/src/nix_error_info.hh``), holding the
    ``nix::ErrorInfo`` -- position, evaluation trace, suggestions -- that
    ``str(exc)`` alone cannot express. They are read defensively because the
    binding translator degrades to a message-only raise if building them
    fails, which must stay a loss of detail rather than an ``AttributeError``.
    """
    if isinstance(exc, NixError):
        return None
    if not type(exc).__module__.startswith("nanopynix_bindings"):
        return None
    type_name = type(exc).__name__
    base = exception_for_nix_type(type_name)
    if base is None:
        return None
    message = str(exc)
    cls, error_type = refine_within(base, message)
    raw = getattr(exc, "raw", "")
    info = getattr(exc, "info", None)
    return cls(error_type, message, raw=raw if isinstance(raw, str) else "", info=info)


def split_type_prefix(message: str) -> tuple[str | None, str]:
    """Split a ``"TypeName: rest"`` worker message into its parts.

    The RPC worker prefixes the originating exception's class name onto the
    gRPC status message (``rpc/worker/_grpc_util.py``). Recovering it is
    strictly better than re-deriving the type from the message body. Returns
    ``(None, message)`` when the prefix is absent or is not a type nanopynix
    knows, so an ordinary message that merely contains a colon is never
    mistaken for a prefixed one.
    """
    head, separator, rest = message.partition(": ")
    if not separator or head not in _NIX_EXCEPTION_TYPES:
        return None, message
    return head, rest


# ════════════════════════════════════════════════════════════════════
# Build failures — structured status, no exception on the wire
# ════════════════════════════════════════════════════════════════════

# Nix's BuildResult::Failure::Status vocabulary -> exception class. These
# strings are produced by `nanopynix::build_result::failure_status_str` in
# `nanopynix-bindings/src/build_result_util.hh` and travel verbatim in
# `BuildResult.status`, so both engines can map them identically.
#
# One producer, deliberately: the header exists because `nix_expr.cpp` and
# `nix_store.cpp` used to render a BuildResult each with their own copy of this
# vocabulary. Two copies free to drift meant the eval route and the store route
# could name the same failure differently, and this table would then hand
# callers two different exception classes for it depending on which route
# reported it. Names not in this table fall back to plain `BuildError`, so a
# drift would have degraded quietly rather than raised.
_BUILD_STATUS_EXCEPTIONS: dict[str, type[BuildError]] = {
    "permanent-failure": PermanentBuildError,
    "input-rejected": InputRejectedError,
    "output-rejected": OutputRejectedError,
    "transient-failure": TransientBuildError,
    "cached-failure": CachedBuildFailureError,
    "timed-out": BuildTimedOutError,
    "misc-failure": MiscBuildError,
    "dependency-failed": DependencyFailedError,
    "log-limit-exceeded": LogLimitExceededError,
    "not-deterministic": NotDeterministicError,
    "no-substituters": NoSubstitutersError,
    "hash-mismatch": BuildHashMismatchError,
}


def build_error_from_result(
    *,
    status: str,
    error_msg: str,
    drv_path: str = "",
) -> BuildError:
    """Build the right :class:`BuildError` subclass from a failed BuildResult.

    Shared by both engines so that an identical build failure raises an
    identical type. *status* is Nix's own failure-status string; an unknown or
    empty one falls back to :class:`BuildError` itself rather than guessing.
    """
    cls = _BUILD_STATUS_EXCEPTIONS.get(status, BuildError)
    message = error_msg or f"build failed with status {status!r}" if status else error_msg or "build failed"
    return cls(cls.__name__, message, status=status, drv_path=drv_path)


def _classify(msg: str, fallback_type: str) -> tuple[type[NixError], str]:
    """Determine the Python exception class and Nix error type from a message.

    Returns ``(exception_class, error_type_name)``.
    """
    clean = strip_ansi(msg)
    for pattern, cls, name in _CLASSIFIERS:
        if pattern.search(clean):
            return cls, name
    # Fallback: use the Python type name from the worker
    return NixError, fallback_type


def from_response(error_type: str, msg: str, *, raw: str = "", info: dict[str, Any] | None = None) -> NixError:
    """Factory: create the right NixError subclass from a worker error response.

    Resolution order, best information first:

    1. *error_type* names a Nix C++ exception type we know -- use it.
    2. *msg* carries a ``"TypeName: "`` prefix from the worker -- use that, and
       strip the prefix so the message reads as Nix wrote it.
    3. Neither -- fall back to matching message patterns. This is boundary C
       (see the module docstring): the type was flattened upstream of us, by
       the daemon protocol, and prose is genuinely all that is left.
    """
    known = exception_for_nix_type(error_type)
    if known is not None:
        cls, refined_type = refine_within(known, msg)
        return cls(refined_type, msg, raw=raw, info=info)

    prefixed_type, remainder = split_type_prefix(msg)
    if prefixed_type is not None:
        cls, refined_type = refine_within(_NIX_EXCEPTION_TYPES[prefixed_type], remainder)
        return cls(refined_type, remainder, raw=raw, info=info)

    cls, classified_type = _classify(msg, error_type)
    return cls(classified_type, msg, raw=raw, info=info)


__all__ = [
    "BadStorePathError",
    "BuildError",
    "BuildHashMismatchError",
    "BuildTimedOutError",
    "CachedBuildFailureError",
    "DependencyFailedError",
    "EvalError",
    "EvalHashMismatchError",
    "EvalSessionClosedError",
    "ForeignValueError",
    "HashMismatchError",
    "InfiniteRecursionError",
    "InputRejectedError",
    "InvalidPathError",
    "LockedFlakeReleasedError",
    "LogLimitExceededError",
    "MiscBuildError",
    "MissingArgumentError",
    "NixAssertionError",
    "NixError",
    "NixSysError",
    "NixTypeError",
    "NoSubstitutersError",
    "NotDeterministicError",
    "ObjectLifetimeError",
    "ObjectMisuseError",
    "OutputRejectedError",
    "ParseError",
    "PermanentBuildError",
    "RestrictedPathError",
    "SessionClosedError",
    "StoreClosedError",
    "StoreError",
    "ThrownError",
    "TransientBuildError",
    "UndefinedVarError",
    "UnimplementedError",
    "UnresolvedValueError",
    "UnsupportedError",
    "UsageError",
    "ValueReleasedError",
    "WrongNixTypeError",
    "build_error_from_result",
    "exception_for_nix_type",
    "from_response",
    "split_type_prefix",
]
