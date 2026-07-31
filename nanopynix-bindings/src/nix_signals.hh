#pragma once
///@file
/// The interrupt token, and the thread-local scope that arms it.
///
/// Nix polls one predicate to decide whether to abandon what it is doing:
///
/// ```cpp
/// // nix/util/signals-impl.hh
/// extern std::atomic<bool> _isInterrupted;                   // process-global
/// extern thread_local std::function<bool()> interruptCheck;  // per-thread
///
/// static inline bool isInterrupted() {
///     return _isInterrupted || (interruptCheck && interruptCheck());
/// }
/// ```
///
/// nanopynix calls `nix::initLibStore`, not `nix::initNix`, so Nix's signal
/// handler thread never starts and `_isInterrupted` is never set in this
/// process. Python owns SIGINT, and that must stay true:
/// `startSignalHandlerThread()` blocks SIGINT process-wide, which would take
/// Ctrl-C away from Python.
///
/// So the per-thread hook is the only lever, and this header is how nanopynix
/// pulls it. A token is a flag that any thread may set. A scope installs a
/// predicate reading that token on the Nix thread, and removes it again.
///
/// See issue #37.

#include <atomic>
#include <memory>

namespace nanopynix {

/// A flag that one thread sets and the Nix thread reads.
///
/// A plain flag and not a reason code. anyio turns a cancellation into
/// `TimeoutError` at the `fail_after` boundary, so a caller never has to tell a
/// timeout from a plain cancel, and every other way a `nix::Interrupted`
/// arrives is a real signal -- which is exactly the case where no token is set.
///
/// Held by `shared_ptr` on both sides: the scope on the Nix thread captures it
/// in a callback that outlives the Python call which armed it.
struct InterruptToken {
    std::atomic<bool> flag{false};

    void cancel() { flag.store(true, std::memory_order_relaxed); }

    void reset() { flag.store(false, std::memory_order_relaxed); }

    [[nodiscard]] bool cancelled() const { return flag.load(std::memory_order_relaxed); }
};

/// Whether the token armed on *this* thread has been cancelled.
///
/// False when no scope is active, which is the signal case. The exception
/// translator in nix_errors.cpp reads this to decide which Python exception a
/// `nix::Interrupted` becomes: a cancellation and a Ctrl-C unwind through the
/// identical C++ exception, and only the token tells them apart.
bool active_scope_was_cancelled();

} // namespace nanopynix
