///@file
/// The three settings objects that `nix.conf` fills in.
///
/// `settings_util.hh` carries the argument. This file is the registration,
/// and it is the shape `src/libcmd/common-eval-args.cc` uses, because that is
/// the file `nix` itself relies on for the same three.
///
/// **Namespace scope, and not a function-local static.** A function-local
/// static registers on its first call, and the first call is a Python one --
/// after `init_libstore()`, which is the call that reads the file. Registering
/// then is registering too late, and the object stays at its compiled default
/// with nothing to say so. A namespace-scope static registers when the shared
/// object loads, which is at `import nanopynix_bindings` and before any Python
/// call.
///
/// **One shared object, so one copy.** `nanopynix_modules.hh` records what a
/// function-local static in a header cost when there were seven of them.

#include "settings_util.hh"

#include <map>
#include <mutex>
#include <string>

// `eval.hh` and not `eval-settings.hh` alone: `EvalSettings::extraPrimOps` is
// a `std::vector<PrimOp>`, and that header only forward-declares `PrimOp`, so
// the vector of a member cannot be constructed from it.
#include <nix/expr/eval.hh>
#include <nix/expr/eval-settings.hh>
#include <nix/fetchers/fetch-settings.hh>
#include <nix/flake/settings.hh>
#include <nix/store/globals.hh>
#include <nix/util/config-global.hh>
#include <nix/util/configuration.hh>

namespace {

/// The three objects `loadConfFile` writes into.
///
/// `nix::settings.readOnlyMode` is the reference libcmd passes, and not a
/// `bool` of our own: this object exists to answer "what does the file say",
/// and read-only mode is a property of the process that the file can also set.
nix::fetchers::Settings configuredFetchSettings;
nix::EvalSettings configuredEvalSettings{nix::settings.readOnlyMode};
nix::flake::Settings configuredFlakeSettings;

const nix::GlobalConfig::Register rConfiguredFetchSettings(&configuredFetchSettings);
const nix::GlobalConfig::Register rConfiguredEvalSettings(&configuredEvalSettings);
const nix::GlobalConfig::Register rConfiguredFlakeSettings(&configuredFlakeSettings);

/// What the file set, taken before anything can reset the bookkeeping.
using Snapshot = std::map<std::string, std::string>;

Snapshot fetchSnapshot;
Snapshot evalSnapshot;
Snapshot flakeSnapshot;
std::once_flag snapshotTaken;

Snapshot overridden_of(const nix::Config &config)
{
    std::map<std::string, nix::Config::SettingInfo> settings;
    config.getSettings(settings, /*overriddenOnly=*/true);
    Snapshot out;
    for (auto &[name, info] : settings)
        out.emplace(name, info.value);
    return out;
}

/// Set each snapshotted value on `target`.
///
/// `target` is the same type as the object the snapshot came from, so every
/// name is one it holds. `set` still answers false for a setting Nix itself
/// refused -- an experimental one whose feature is off -- and Nix has already
/// warned about that, so this does not raise on top of the warning.
void replay(nix::Config &target, const Snapshot &snapshot)
{
    for (auto &[name, value] : snapshot)
        target.set(name, value);
}

} // namespace

void snapshot_nix_conf()
{
    std::call_once(snapshotTaken, [] {
        fetchSnapshot = overridden_of(configuredFetchSettings);
        evalSnapshot = overridden_of(configuredEvalSettings);
        flakeSnapshot = overridden_of(configuredFlakeSettings);
    });
}

void apply_nix_conf(nix::fetchers::Settings &settings)
{
    replay(settings, fetchSnapshot);
}

void apply_nix_conf(nix::EvalSettings &settings)
{
    replay(settings, evalSnapshot);
}

void apply_nix_conf(nix::flake::Settings &settings)
{
    replay(settings, flakeSnapshot);
}
