#pragma once
///@file
/// One setting override map, applied to one Nix `Config`.
///
/// Two areas take a `dict[str, str]` of settings from Python and push it into
/// a freshly constructed `Config` subclass: `nix_flake.cpp` for
/// `fetchers::Settings` and `flake::Settings`, and `nix_fetchers.cpp` for the
/// `fetchers::Settings` that the registry reads. A fresh instance is not the
/// one that `GlobalConfig` registers, so `nix.conf` does not reach it and the
/// caller is the only source of a non-default value.

#include <map>
#include <stdexcept>
#include <string>

#include <nix/util/configuration.hh>

/// Set each named setting on `config`, and raise when a name is unknown.
inline void apply_settings_overrides(
    nix::Config &config, const std::map<std::string, std::string> &overrides)
{
    for (auto &[name, value] : overrides)
        if (!config.set(name, value))
            throw std::runtime_error("unknown setting: " + name);
}
