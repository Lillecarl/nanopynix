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
/// nix_expr.cpp defines this. The declaration is here so that py_eval.hh can
/// call it without including the whole of that translation unit.

/// Register the calling thread with Boehm, if it is not registered already,
/// and keep it registered until the thread exits. Idempotent.
void nanopynix_ensure_gc_thread_registered();
