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
#include <utility>
#include <variant>

#include <nix/store/build-result.hh>
#include <nix/store/derived-path.hh>
#include <nix/store/realisation.hh>
#include <nix/store/store-dir-config.hh>
#include <nix/util/signature/local-keys.hh>
#include <nix/util/util.hh>

#include <nanopynix/nix_compat_config.hh>

namespace nb = nanobind;

namespace nanopynix::build_result {

/// Nix's own decomposition of the derived path a result is keyed by.
///
/// `KeyedBuildResult::path` is a `nix::DerivedPath` -- a sum type whose `^`
/// string is its serialization, not a store path. Rendering it with
/// `to_string` and calling the field `drv_path` is what this used to do, and
/// it meant a build of a bare `.drv` reported `drv_path` as `<drv>^*`: not a
/// store path, not readable by `models.StorePath`'s accessors, and not
/// accepted by `nanopynix_helpers.fod.derivation_name_from_path`, which
/// requires a `.drv` suffix. The name promised a store path and the value was
/// something else. The worker is the only layer holding the real sum type, so
/// it emits the parts rather than a string every caller has to re-split.
///
/// `outputs` distinguishes the two variants rather than leaving the caller to
/// infer them:
///
/// - `[]` -- `DerivedPath::Opaque`. "Fetch this path", no build.
/// - `["*"]` -- `DerivedPath::Built` with `OutputsSpec::All`. Every output.
///   `*` is the spelling `OutputsSpec::to_string` uses and is not a legal
///   output name, so it cannot collide with a real one.
/// - named outputs -- `OutputsSpec::Names`, in Nix's own sorted order.
///
/// One case leaves `drv_path` something other than a plain store path: with
/// `dynamic-derivations`, `Built::drvPath` is itself a `SingleDerivedPath`
/// that may be `Built`, and `SingleDerivedPath::to_string` renders that as
/// `<drv>^<output>` -- a derivation that must itself be built first. That is
/// Nix's own serialization of a thing which genuinely is not a store path;
/// flattening it would drop the information this change exists to keep.
inline std::pair<std::string, nb::list> derived_path_parts(
        const nix::DerivedPath &path, const nix::StoreDirConfig &store) {
    nb::list outputs;
    if (const auto *built = std::get_if<nix::DerivedPath::Built>(&path.raw())) {
        std::visit(
                nix::overloaded{
                        [&](const nix::OutputsSpec::All &) { outputs.append(nb::str("*")); },
                        [&](const nix::OutputsSpec::Names &names) {
                            for (const auto &name : names) outputs.append(nb::str(name.c_str()));
                        },
                },
                built->outputs.raw);
        return {built->drvPath->to_string(store), outputs};
    }
    return {std::get<nix::DerivedPath::Opaque>(path.raw()).to_string(store), outputs};
}

/// What the build produced, keyed by output name.
///
/// `nix build` answers its own output paths from `builtOutputs` and from
/// nothing else, so a caller without this field has to query the store again
/// to find its own build. The map is empty for a `DerivedPath::Opaque`
/// request, and populated for every other one -- an input-addressed
/// derivation included.
///
/// **The value type of the map differs across the supported Nix versions, and
/// this needs no branch.** 2.34 declares `std::map<OutputName, Realisation>`
/// and 2.35 changed it to `std::map<OutputName, UnkeyedRealisation>`.
/// `Realisation` derives from `UnkeyedRealisation`, and both fields read here
/// are members of the base, so one expression compiles against all three.
///
/// The 2.34-only `Realisation::id` stays unread on purpose: it is a
/// `DrvOutput` naming the derivation the caller already holds, and its own
/// shape changed in the same release (`Hash drvHash` became
/// `StorePath drvPath`).
///
/// **`BuildResult`'s timing fields are absent for a different reason.**
/// `timesBuilt`, `startTime`, `stopTime`, `cpuUser` and `cpuSystem` never
/// reach a caller of `buildPathsWithResults` on 2.34 or 2.35.
/// `Goal::doneSuccess` assigns `buildResult.inner` alone, and
/// `DerivationTrampolineGoal` -- the goal those versions hand back -- finishes
/// by building a fresh `Success` from a status and this map, so every timing
/// field keeps its default of `0` or `nullopt`. Nix `git` corrects it, by
/// copying the whole child result first. Reporting a build that took zero
/// seconds on two of three versions is worse than reporting nothing, so
/// nothing here reports them. Issue #142 holds the measurement.
inline nb::dict built_outputs_to_dict(const auto &builtOutputs, const nix::StoreDirConfig &store) {
    nb::dict out;
    for (const auto &[name, realisation] : builtOutputs) {
        nb::dict entry;
        entry["out_path"] = store.printStorePath(realisation.outPath);
        nb::list signatures;
        for (const auto &signature : nix::Signature::toStrings(realisation.signatures))
            signatures.append(signature);
        entry["signatures"] = signatures;
        out[name.c_str()] = entry;
    }
    return out;
}

/// The six fields `nanopynix.exceptions.build_error_from_result` and
/// `nanopynix.models.BuildResult` read.
inline nb::dict to_dict(
        const std::string &drv_path,
        const nb::list &outputs,
        bool success,
        const std::string &status,
        const std::string &error_msg,
        const nb::dict &built_outputs) {
    nb::dict d;
    d["drv_path"] = drv_path;
    d["outputs"] = outputs;
    d["success"] = success;
    d["status"] = status;
    d["error_msg"] = error_msg;
    d["built_outputs"] = built_outputs;
    return d;
}

/// Nix splits the build status into a success half and a failure half.
///
/// Nix 2.31 had one flat enum instead, and no `HashMismatch` at all: that
/// member arrived with the split. Issue #126 raised the supported floor to
/// 2.34, so the flat form is gone from here.
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
    auto [path, outputs] = derived_path_parts(kbr.path, store);
    if (auto *success = kbr.tryGetSuccess())
        return to_dict(
                path,
                outputs,
                true,
                success_status_str(success->status),
                "",
                built_outputs_to_dict(success->builtOutputs, store));
    // A failed build produced nothing, and Nix carries no `builtOutputs` on
    // the failure variant at all.
    if (auto *failure = kbr.tryGetFailure())
        return to_dict(path, outputs, false, failure_status_str(failure->status), failure->msg(), nb::dict());
    return to_dict(path, outputs, false, "unknown", "", nb::dict());
}

}  // namespace nanopynix::build_result
