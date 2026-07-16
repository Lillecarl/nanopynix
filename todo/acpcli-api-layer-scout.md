# Focused API-layer scout

You are the coordinator of a read-only architecture reconnaissance. Do not edit
files, run tests, change settings, inspect unrelated directories, or propose
implementation patches. Your sole deliverable is a concise evidence-backed
report that lets the parent agent choose one small next API increment.

Before reading source, spawn exactly three read-only subagents. Give each its
own task below verbatim. Do not spawn more agents. Do not ask subagents to
produce directory trees, summaries of the whole repository, or general Nix
background. A subagent must read only its listed files and report only exact
method/type gaps with file and line references.

Subagent A — public transports

Read only `python/src/nanopynix/protocols.py`, `inproc.py`, `store.py`, and
`nix.py`. Compare the shared Protocol surface with the two public Store and
Session facades. Identify at most three operations that are already supported
by both transports but lack an ergonomic shared public method or model. For
each: current method names, source lines, proposed public signature, return
type, and one semantic risk. Do not propose generated-RPC forwarding.

Subagent B — raw capabilities and wire contracts

Read only `bindings/src/nix_store.cpp`, `bindings/src/nanopynix_store.pat`,
`proto/store.proto`, `proto/common.proto`, and
`python/src/nanopynix/_worker_store.py`. Identify at most three operations
where L1 and RPC already represent the same Nix concept but return mismatched
or awkward Python shapes. For each: exact L1 and RPC names, wire/result
shapes, whether a public model already exists, and the smallest normalisation
that preserves Nix semantics. Do not suggest C++ or proto changes unless both
existing paths genuinely cannot expose the operation safely.

Subagent C — eval and flake parity

Read only `python/src/nanopynix/protocols.py`, `inproc.py`, `_session.py`,
`nix.py`, `models.py`, and `tests/nanopynix/test_protocols.py`. Compare the
L2 in-process and L3 worker-backed EvalSession, ReplSession, Value, and locked
flake interfaces. Identify at most three high-value public operations or
return-model inconsistencies that can be unified without weakening lifetime or
thread-affinity guarantees. Give exact source references and a minimal test
strategy.

After all three reports return, synthesize no more than three candidate next
increments. Rank them by user value and implementation risk. For the top
candidate provide a file-by-file plan, explicit non-goals, and focused tests.
Do not start implementation. If a subagent wanders beyond scope, discard that
part of its report rather than investigating it yourself.
