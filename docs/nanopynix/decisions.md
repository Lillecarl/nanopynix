# Decisions: rejected ideas and open questions

This page records what a code review of 2026-07 considered and did not turn
into work, and the questions that a maintainer must answer. An idea here has a
reason, so that the next reader does not propose it again without new
evidence.

## Rejected or deferred ideas

**Split `inproc/_impl.py` before the interfaces exist.** Rejected for now.
Splitting turns 43 same-module private accesses into 43 cross-module ones,
which is worse. Introduce the interfaces first, and split second. See Q7 below.

**Replace `getattr`-based RPC proxying with generated code.** Deferred.
`RpcProxyMixin` and `worker_op` already collapse the two-method-per-RPC
pattern, and the generated betterproto2 bases give the message types. The
remaining `getattr(self._worker.eval_stub, method_name)` is one line and is
covered by the proxy's own tests. Revisit only if the RPC count grows again.

**A `WorkerBusyError` for overlapping evaluator calls** (`ROADMAP.md` item 1).
Deferred, and possibly obsolete. `EvalProxy._operation_lock` already serialises
evaluator calls, and store calls already overlap. The ROADMAP item was written
when the two shared one lane. Confirm the current behaviour is what the
maintainer wants before adding an exception for it.

**Remove the `capture=True` return unions** (`ROADMAP.md` item 3). Already
done, as far as this review can see: `LogCapture` is the context-manager form
the item asks for, and no store or eval method returns a union with a capture.
Delete the ROADMAP item rather than implementing it.

**A "pure Python" model layer beside the betterproto2 messages.** Rejected.
The wire type is the domain type here, and `_status_details.py`'s reasoning
about `info` staying a `dict` shows the cost of a parallel representation.

**Pin dependency versions in `pyproject.toml`.** Deferred, and it is an open
question (Q6 below). Nix pins everything today, and a PyPI-facing bound would
be a second source of truth that nothing checks.

**Add a protocol version to `InitRequest`.** Deferred. The client spawns the
worker from the same installed package, so skew cannot happen. It becomes
necessary only if the stdio entry point gains a client. Record the reasoning in
`worker.proto` so the next reader does not have to re-derive it.

**Change the store executor from `anyio.to_thread` to a dedicated pool.**
Rejected. Nix stores are thread-safe, the four-slot `CapacityLimiter` bounds
the work, and the thread-local logger request id makes correlation correct.
There is no problem to solve.

---

## Open questions

Each question has a recommended default. A question stays here until a
maintainer decides.

**Q1 — Should a `NixSettings` field outside the global scope be a session
default, or an error?**
This decides the shape of #6. A router (recommended) keeps the catch-all
the design chose and makes `pure_eval` settable once per session. Rejection is
half the work and leaves callers repeating `eval_settings=` at every call.
*Recommended default: the router.*

**Q2 — May `_core` import `nanopynix.settings`?**
It already does, for `SettingsProvenance` and for the rejection helpers, and
The fix in #8 adds one more use. The alternative is to pass the guard in as a
callback, which is indirection for a rule that is genuinely a property of Nix.
*Recommended default: yes, and say so in the layering rules.*

**Q3 — Is lossless log delivery worth an unbounded stall of the evaluator?**
The fix in #13 trades one for the other. A build that emits more than 10 000
events into a stalled consumer currently stops.
*Recommended default: bound the wait generously, drop, and count.*

**Q4 — Does the stdio worker have a future?**
It has an entry point, no client, a stale docstring and no test. Keeping it
means giving it a protocol version and a compatibility rule (Q5).
*Recommended default: delete it; add it back with a client when one exists.*

**Q5 — Is the RPC protocol a public interface?**
`common.proto`'s comments are written for an outside reader ("readable by any
language's gRPC tooling"). If that is the intent, the protocol needs a version
field, a compatibility policy and a changelog. If it is internal, say so at the
top of each `.proto`.
*Recommended default: internal for now, stated explicitly.*

**Q6 — Should `nanopynix` carry dependency bounds for PyPI?**
Nix pins everything the project builds and tests against, so bounds would be
unverified claims. But `pydantic`, `betterproto2` and `grpclib` are all
libraries where a major release breaks callers.
*Recommended default: add lower bounds only, for the four libraries whose APIs
the code actually uses; leave upper bounds to Nix.*

**Q7 — How much of `inproc`'s cross-class private access is worth removing?**
The review proposes interfaces and a split. The smaller step it named — adopting
rpc's `claim_eval`/`release_eval` spelling — is gone, and the reason is worth
recording. #36 asked whether each rpc-only name is API at all, and those two
were not: they add and discard the session's own set of open evaluators, and a
caller can do nothing with either. So rpc adopted inproc's direction instead of
the other way round, and they are `_claim_eval` and `_release_eval` now.

That leaves the question standing with the cheap answer removed. What remains
is the interfaces and the split.
*Recommended default: decide on the split on its own evidence, and do not treat
a spelling change as a step towards it.*

