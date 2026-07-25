#pragma once

/// Two Nix errors nanopynix raises that Nix has no reusable API for.
///
/// Both correspond to something Nix itself reports, but only from inside the
/// evaluator, via code paths with no callable entry point:
/// `ExprSelect::eval` for a missing attribute and `prim_elemAt` for an
/// out-of-range list index. `Value.attr()` / `Value.list_get()` are not those
/// code paths, so nanopynix has to construct the equivalent.
///
/// They exist as *distinct C++ types* purely so the single translator in
/// `nix_errors.cpp` can tell them apart from a generic `nix::EvalError` and
/// pick a Python class that is also a `KeyError` / `IndexError`. Deriving from
/// `nix::EvalError` is what keeps them inside the hierarchy the translator
/// already owns -- and means the catch chain must list them *before* the
/// `nix::EvalError` arm, which `-Wexceptions` enforces.

#include <nix/expr/eval-error.hh>
#include <nix/util/suggestions.hh>

#include <utility>

namespace nanopynix {

/// `value.attr("nope")` -- the attribute is absent from the attrset.
///
/// Carries Nix's "Did you mean ...?" suggestions, which is the whole reason
/// this is built in C++ rather than raised from Python: the candidate set is
/// the attrset's symbol table, and `nix::Suggestions::bestMatches` is the same
/// ranking Nix's own `attribute 'x' missing` error uses. Reconstructing either
/// on the Python side would mean shipping every attribute name across the
/// boundary just to throw them away.
struct MissingAttributeError : nix::EvalError {
    using nix::EvalError::EvalError;

    /// `err` is `protected` on `nix::BaseError`, so a subclass is the only
    /// place suggestions can be attached without going through Nix's
    /// `EvalErrorBuilder` -- which is reachable only via `EvalState::error<T>`
    /// and would drag in a position we do not have. There is no Nix source
    /// location for a Python-side `value.attr(...)` call.
    void with_suggestions(nix::Suggestions suggestions) {
        err.suggestions = std::move(suggestions);
    }
};

/// `value.list_get(99)` on a shorter list.
struct ListIndexError : nix::EvalError {
    using nix::EvalError::EvalError;
};

} // namespace nanopynix
