#pragma once
///@file
/// The four fetcher entry points that dropped their settings parameter.
///
/// **Upstream `d423f75c5` removed the leading `const fetchers::Settings &`**
/// from `parseFlakeRef`, from `FlakeRef::fromAttrs`, from `Input::fromURL` and
/// from `Input::fromAttrs`. The settings now reach the fetcher another way.
/// Every other member that takes settings keeps it: `FlakeRef::resolve`,
/// `FlakeRef::lazyFetch`, `Input::getAccessor` and `Input::isLocked` are
/// unchanged, so this header covers four names and no more.
///
/// **Each wrapper tests the call, and never the version.** The `git` lane of
/// CI builds an unpinned Nix on purpose, to find a break like this early, and
/// every git revision since 2.35 reports `2.36pre<date>_<rev>`. A `#if` on
/// `NANOPYNIX_NIX_VERSION_NUMBER` cannot separate a revision before that
/// commit from one after it.
///
/// **Call these namespace-qualified.** `nix_fetchers.cpp` has a static
/// `input_from_url` of its own, and `nix_flake.cpp` has a `parse_flake_ref`.
/// Do not write `using namespace nanopynix::nix_compat` in either file.

#include <utility>

#include <nix/fetchers/fetchers.hh>
#include <nix/flake/flakeref.hh>

namespace nanopynix::nix_compat {

/// The settings parameter is first, so a caller passes it on every version.
///
/// The `requires` expression asks whether the call compiles without it. On a
/// Nix that still takes settings the answer is no, because nothing converts a
/// URL to a `Settings`, and the second branch applies.
template<typename... Args>
auto parse_flake_ref(const nix::fetchers::Settings &settings, Args &&...args) {
    if constexpr (requires { nix::parseFlakeRef(std::forward<Args>(args)...); })
        return nix::parseFlakeRef(std::forward<Args>(args)...);
    else
        return nix::parseFlakeRef(settings, std::forward<Args>(args)...);
}

template<typename... Args>
auto flake_ref_from_attrs(const nix::fetchers::Settings &settings, Args &&...args) {
    if constexpr (requires { nix::FlakeRef::fromAttrs(std::forward<Args>(args)...); })
        return nix::FlakeRef::fromAttrs(std::forward<Args>(args)...);
    else
        return nix::FlakeRef::fromAttrs(settings, std::forward<Args>(args)...);
}

template<typename... Args>
auto input_from_url(const nix::fetchers::Settings &settings, Args &&...args) {
    if constexpr (requires { nix::fetchers::Input::fromURL(std::forward<Args>(args)...); })
        return nix::fetchers::Input::fromURL(std::forward<Args>(args)...);
    else
        return nix::fetchers::Input::fromURL(settings, std::forward<Args>(args)...);
}

template<typename... Args>
auto input_from_attrs(const nix::fetchers::Settings &settings, Args &&...args) {
    if constexpr (requires { nix::fetchers::Input::fromAttrs(std::forward<Args>(args)...); })
        return nix::fetchers::Input::fromAttrs(std::forward<Args>(args)...);
    else
        return nix::fetchers::Input::fromAttrs(settings, std::forward<Args>(args)...);
}

} // namespace nanopynix::nix_compat
