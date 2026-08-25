#pragma once
///@file
/// What `nix.conf` says, and one setting override map, applied to a `Config`.
///
/// **Nix keeps four settings registries and `globalConfig` is only one of
/// them.** `tests/meta` pins that they are disjoint, measured on every
/// supported version. `initLibStore` calls `loadConfFile(globalConfig)`, so
/// `nix.conf` reaches exactly what is registered there and nothing else.
///
/// libcmd is what registers the other three, and nanopynix does not link
/// libcmd:
///
/// ```cpp
/// // src/libcmd/common-eval-args.cc
/// fetchers::Settings fetchSettings;
/// static GlobalConfig::Register rFetchSettings(&fetchSettings);
/// ```
///
/// Without that, every `EvalSettings`, `fetchers::Settings` and
/// `flake::Settings` this library builds took its compiled default whatever
/// the file said, and the caller was the only source of a non-default value.
/// Measured before the fix, with one `NIX_CONFIG` carrying settings from two
/// registries: `http-connections = 7` arrived, and `pure-eval = true` in the
/// same string did not reach an evaluator, so `builtins.currentSystem`
/// resolved where a pure evaluator has no such builtin. Issue #234.
///
/// `settings_util.cpp` registers one object of each of the three kinds, for
/// `loadConfFile` to fill in. `apply_nix_conf` then copies what the file set
/// onto a freshly constructed instance, which keeps the rule that a binding
/// constructs its own settings: it now starts from what Nix read rather than
/// from the compiled default.
///
/// **Order at a call site: `apply_nix_conf` first, then
/// `apply_settings_overrides`.** The caller wins over the file, which is what
/// `nix` does and what issue #234 requires.

#include <map>
#include <stdexcept>
#include <string>

#include <nix/util/configuration.hh>

namespace nix {
struct EvalSettings;
namespace fetchers {
struct Settings;
}
namespace flake {
struct Settings;
}
} // namespace nix

/// Record what `nix.conf` set, for `apply_nix_conf` to replay.
///
/// **Called from `init_libstore`, and it cannot wait.** `Config` marks a
/// setting `overridden` when something writes it, and that flag is how the
/// file's values are told apart from the compiled defaults. `_nix_core.py`
/// then calls `reset_overridden()` on purpose, so that a later
/// `list_settings(overridden_only=True)` reports what nanopynix applied and
/// not what the file did -- and `GlobalConfig::resetOverridden` clears the
/// flag on *every* registered object, these three included. So the flag is
/// gone before the first evaluator is built, and a copy that read it then
/// copied nothing. Measured: `pure-eval = true` reached `globalConfig` and
/// still did not reach an evaluator built through `nanopynix.inproc`.
///
/// Taking the values here, immediately after `loadConfFile` and before any
/// Python code runs, is what survives that reset.
///
/// Runs once. `initLibStore` is idempotent, so a second call reads a state
/// that the reset has already been through, and replacing the snapshot with
/// it would empty the snapshot.
void snapshot_nix_conf();

/// Copy every setting `nix.conf` set onto `settings`.
///
/// Only the overridden ones, so a value the file did not name keeps the
/// default of the fresh instance rather than being written over with the
/// same default.
///
/// **An experimental setting can be dropped here, and Nix drops it in the
/// same place for the same reason.** `BaseSetting::set` warns and keeps the
/// default when the gating feature is off, so a setting the file names is
/// silently absent until the feature is on. Issue #264 carries the
/// measurement.
void apply_nix_conf(nix::fetchers::Settings &settings);
void apply_nix_conf(nix::EvalSettings &settings);
void apply_nix_conf(nix::flake::Settings &settings);

/// Set each named setting on `config`, and raise when a name is unknown.
inline void apply_settings_overrides(
    nix::Config &config, const std::map<std::string, std::string> &overrides)
{
    for (auto &[name, value] : overrides)
        if (!config.set(name, value))
            throw std::runtime_error("unknown setting: " + name);
}
