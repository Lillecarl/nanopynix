#pragma once
///@file
/// Boehm must know every thread that drives an evaluator.
///
/// `GC_push_all_stacks` walks Boehm's thread table, pushes the stack of each
/// entry as a root, and never visits a thread that is not in it. So a thread
/// that allocates Nix values without a registration either dies with
/// "Collecting from unknown thread", when a collection starts on it, or loses
/// the values that only its own stack refers to, when a collection starts
/// somewhere else.
///
/// `PyEvalState::checkThread` answers a different question -- *which* thread
/// may drive an evaluator -- and it does not make that thread known to the
/// collector.
///
/// nix_expr.cpp defines both. The declarations are here so that py_eval.hh can
/// call them without including the whole of that translation unit.

/// Start the collector, on a thread that owns it and never exits. Idempotent.
///
/// Boehm keeps its one static `first_thread` entry for whoever calls
/// `GC_INIT()`, and removes it at no point, so that thread must outlive every
/// collection. See `nanopynix_start_gc_owner_thread` in nix_expr.cpp for the
/// measurement.
///
/// **Call this before `nanopynix_ensure_gc_thread_registered`.** A thread
/// cannot register with a collector that has not started: `GC_INIT` is what
/// runs `GC_allow_register_threads`, and `GC_register_my_thread` aborts the
/// process without it.
void nanopynix_start_gc_owner_thread();

/// Register the calling thread with Boehm, if it is not registered already,
/// and keep it registered until the thread exits. Idempotent.
void nanopynix_ensure_gc_thread_registered();
