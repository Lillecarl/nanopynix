#pragma once

/// One rendering of Nix's BuildResult, for both files that report one.
///
/// Nix 2.32 split `BuildResult::Status` into `Success::Status` and
/// `Failure::Status`, so reading a result takes a version branch. That branch
/// and the vocabulary it renders used to be duplicated verbatim in
/// `nix_expr.cpp` and `nix_store.cpp` -- around a hundred lines each, in two
/// translation units with no way to notice they had drifted, plus a
/// `NANOPYNIX_NIX_HAS_KEYED_BUILD_RESULTS` macro defined and `#undef`ed
/// separately in both to name the condition. With one copy the condition can
/// just be written where it is used, so the macro is gone.
///
/// The strings are not an internal detail. `nanopynix.exceptions`'s
/// `_BUILD_STATUS_EXCEPTIONS` maps them to `BuildError` subclasses, so they are
/// what decides which class a caller's `except` clause has to name. Two copies
/// meant the eval route and the store route could come to disagree about the
/// name of one failure, and `build_error_from_result` would then hand the same
/// failure to callers as two different types depending on which route reported
/// it.

#include <nanobind/nanobind.h>

#include <string>

#include <nix/store/build-result.hh>
#include <nix/store/derived-path.hh>

#include <nanopynix/nix_compat_config.hh>

namespace nb = nanobind;

namespace nanopynix::build_result {

/// The four fields `nanopynix.exceptions.build_error_from_result` reads.
inline nb::dict to_dict(
        const std::string &drv_path,
        bool success,
        const std::string &status,
        const std::string &error_msg) {
    nb::dict d;
    d["drv_path"] = drv_path;
    d["success"] = success;
    d["status"] = status;
    d["error_msg"] = error_msg;
    return d;
}

#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_32
/// Pre-2.32 Nix has one flat status enum covering success and failure alike.
///
/// It has no `HashMismatch`: that member arrived with the 2.32 split, so a
/// fixed-output hash mismatch reports under one of the neighbouring failure
/// names here. The Python side maps whatever it is given and falls back to
/// plain `BuildError` for a name it does not know, so the older vocabulary
/// being shorter costs detail rather than correctness.
inline std::string status_str(nix::BuildResult::Status s) {
    using enum nix::BuildResult::Status;
    switch (s) {
        case Built:                  return "built";
        case Substituted:            return "substituted";
        case AlreadyValid:           return "already-valid";
        case ResolvesToAlreadyValid: return "resolves-to-already-valid";
        case PermanentFailure:       return "permanent-failure";
        case InputRejected:          return "input-rejected";
        case OutputRejected:         return "output-rejected";
        case TransientFailure:       return "transient-failure";
        case CachedFailure:          return "cached-failure";
        case TimedOut:               return "timed-out";
        case MiscFailure:            return "misc-failure";
        case DependencyFailed:       return "dependency-failed";
        case LogLimitExceeded:       return "log-limit-exceeded";
        case NotDeterministic:       return "not-deterministic";
        case NoSubstituters:         return "no-substituters";
    }
    return "unknown";
}

inline nb::dict from_kbr(const nix::KeyedBuildResult &kbr, const nix::StoreDirConfig &store) {
    auto result = static_cast<nix::BuildResult>(kbr);
    return to_dict(kbr.path.to_string(store), result.success(), status_str(result.status), result.errorMsg);
}
#else
inline std::string success_status_str(nix::BuildResult::Success::Status s) {
    using enum nix::BuildResult::Success::Status;
    switch (s) {
        case Built:                  return "built";
        case Substituted:            return "substituted";
        case AlreadyValid:           return "already-valid";
        case ResolvesToAlreadyValid: return "resolves-to-already-valid";
    }
    return "unknown";
}

inline std::string failure_status_str(nix::BuildResult::Failure::Status s) {
    using enum nix::BuildResult::Failure::Status;
    switch (s) {
        case PermanentFailure:  return "permanent-failure";
        case InputRejected:     return "input-rejected";
        case OutputRejected:    return "output-rejected";
        case TransientFailure:  return "transient-failure";
        case CachedFailure:     return "cached-failure";
        case TimedOut:          return "timed-out";
        case MiscFailure:       return "misc-failure";
        case DependencyFailed:  return "dependency-failed";
        case LogLimitExceeded:  return "log-limit-exceeded";
        case NotDeterministic:  return "not-deterministic";
        case NoSubstituters:    return "no-substituters";
        case HashMismatch:      return "hash-mismatch";
    }
    return "unknown";
}

inline nb::dict from_kbr(const nix::KeyedBuildResult &kbr, const nix::StoreDirConfig &store) {
    auto path = kbr.path.to_string(store);
    if (auto *success = kbr.tryGetSuccess())
        return to_dict(path, true, success_status_str(success->status), "");
    if (auto *failure = kbr.tryGetFailure())
        return to_dict(path, false, failure_status_str(failure->status), failure->msg());
    return to_dict(path, false, "unknown", "");
}
#endif

}  // namespace nanopynix::build_result
