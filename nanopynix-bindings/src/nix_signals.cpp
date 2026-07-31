/// The interrupt token and its scope, bound to Python.
///
/// See nix_signals.hh for why the per-thread hook is the only lever nanopynix
/// has, and issue #37 for what it is for.

#include <functional>
#include <memory>
#include <thread>
#include <utility>

#include <nanobind/nanobind.h>
#include <nanobind/stl/shared_ptr.h>

#include <nix/util/signals.hh>

#include "nanopynix_modules.hh"
#include "nix_signals.hh"

namespace nb = nanobind;
using namespace nb::literals;

namespace nanopynix {

namespace {

/// The token armed on this thread, or null.
///
/// A raw pointer, not a `shared_ptr`: the `InterruptScope` that sets it owns a
/// `shared_ptr` for its whole lifetime and clears this on the way out, so the
/// pointee always outlives the pointer.
thread_local InterruptToken *current_token = nullptr;

/// Arms `token` on the calling thread for as long as this object lives.
///
/// **Composes with the predicate already installed, and never replaces it.**
/// Nix assigns `interruptCheck` itself, in `src/libutil/thread-pool.cc`:
///
/// ```cpp
/// if (!mainThread)
///     unix::interruptCheck = [&]() { return (bool) quit; };
/// ```
///
/// The `!mainThread` guard is the reason a scope is safe to enter at all: a
/// `ThreadPool` writes the slot of its own workers, never the slot of the
/// thread that called `process()`. So a scope on a Nix thread and a pool
/// running under it do not collide today.
///
/// Composing is what keeps that true without depending on it. It makes a
/// nested scope correct, it survives a Nix version that drops the guard, and
/// it costs one call. Restoring on the way out matters for the same reason:
/// the predicate that Nix installs captures `&quit` by reference, so a slot
/// left holding it after the pool dies would read freed memory.
///
/// One limit follows, and no composition removes it. Work that a Nix
/// `ThreadPool` runs on *its* threads reads *its* predicate, not this token.
/// Such work stops when Nix stops it, and not when a caller cancels.
class InterruptScope {
public:
    explicit InterruptScope(std::shared_ptr<InterruptToken> token)
        : token_(std::move(token)), previous_(nix::unix::interruptCheck), previous_token_(current_token),
          owner_(std::this_thread::get_id()) {
        current_token = token_.get();
        nix::unix::interruptCheck = [token = token_, prev = previous_](
                                    ) { return token->cancelled() || (prev && prev()); };
    }

    /// Restores the previous predicate, but only on the thread that armed it.
    ///
    /// `interruptCheck` and `current_token` are `thread_local`. A destructor
    /// that ran elsewhere -- a scope entered but never exited, then collected
    /// by Python on another thread -- would write *that* thread's slot and
    /// leave the armed one set for ever. Leaking the predicate on the owning
    /// thread is the lesser fault, and the token it holds is inert once the
    /// caller drops it.
    ~InterruptScope() {
        if (std::this_thread::get_id() != owner_) {
            return;
        }
        nix::unix::interruptCheck = previous_;
        current_token = previous_token_;
    }

    InterruptScope(const InterruptScope &) = delete;
    InterruptScope &operator=(const InterruptScope &) = delete;
    InterruptScope(InterruptScope &&) = delete;
    InterruptScope &operator=(InterruptScope &&) = delete;

private:
    std::shared_ptr<InterruptToken> token_;
    std::function<bool()> previous_;
    InterruptToken *previous_token_;
    std::thread::id owner_;
};

/// The Python-facing context manager. `__enter__` arms, `__exit__` disarms.
///
/// Separate from `InterruptScope` because nanobind needs an object it can
/// construct without arming anything: Python builds it first and enters it
/// afterwards, and the arming has to happen on the thread that runs the body.
class PyInterruptScope {
public:
    explicit PyInterruptScope(std::shared_ptr<InterruptToken> token) : token_(std::move(token)) {}

    void enter() {
        if (scope_) {
            throw nb::value_error("interrupt scope is already active");
        }
        scope_ = std::make_unique<InterruptScope>(token_);
    }

    void exit() { scope_.reset(); }

private:
    std::shared_ptr<InterruptToken> token_;
    std::unique_ptr<InterruptScope> scope_;
};

} // namespace

bool active_scope_was_cancelled() {
    return current_token != nullptr && current_token->cancelled();
}

} // namespace nanopynix

void nanopynix_bind_signals(nb::module_ &m) {
    m.doc() =
        "nanopynix: the per-thread interrupt hook that lets a caller stop Nix "
        "work. Arm a token around a call and set the token from any thread; "
        "Nix raises at its next checkInterrupt(). Note that libexpr has almost "
        "none: a fetch, a store operation or a build stops, and a pure "
        "evaluation does not.";

    nb::class_<nanopynix::InterruptToken>(m, "InterruptToken")
        .def(nb::init<>())
        .def("cancel", &nanopynix::InterruptToken::cancel,
             "Ask the armed thread to stop. Safe to call from any thread.")
        .def("reset", &nanopynix::InterruptToken::reset, "Clear the reason so the token can be armed again.")
        .def_prop_ro("cancelled", &nanopynix::InterruptToken::cancelled);

    nb::class_<nanopynix::PyInterruptScope>(m, "interrupt_scope")
        .def(nb::init<std::shared_ptr<nanopynix::InterruptToken>>(), "token"_a)
        .def("__enter__",
             [](nanopynix::PyInterruptScope &self) {
                 self.enter();
                 return &self;
             })
        // `nb::args`, not three `nb::handle` parameters: CPython passes
        // `(None, None, None)` on the non-exception path, and nanobind rejects
        // None for a `handle` parameter. That failure surfaces as a TypeError
        // *from `__exit__`*, which replaces whatever the body raised and, worse,
        // leaves the scope armed.
        //
        // Returns nothing, rather than `false`. The two are the same to
        // CPython, which only tests the result for truth. They are not the
        // same to a type checker: a `__exit__` typed `-> bool` may suppress
        // the exception, so every `with` block over this scope reads as one
        // that can fall through. `-> None` says what this scope really does.
        .def("__exit__", [](nanopynix::PyInterruptScope &self, nb::args) { self.exit(); });
}
