#pragma once

#include <memory>

#include <nix/util/logging.hh>

#include <nanopynix/nix_compat_config.hh>

namespace nanopynix::nix_compat {

inline std::unique_ptr<nix::Logger> & raw_logger_owner() {
    static std::unique_ptr<nix::Logger> owner;
    return owner;
}

inline nix::Logger * logger() {
#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_35
    return nix::logger.get();
#else
    return nix::logger;
#endif
}

/// Put a logger in `nix::logger`. **Call this once for the whole process.**
///
/// There used to be a `restore_simple_logger` beside this, and `remove_logger`
/// called it at the end of every session. That freed the logger the session
/// installed, and Nix's curl file-transfer thread reads a `Logger &` on its
/// own schedule with no way to join it, so the free raced the read (issue
/// #66). `nix_util.cpp` installs one leaked `PyLogger` at module import now,
/// and attaches and detaches the Python callback inside it.
inline void install_logger(std::unique_ptr<nix::Logger> new_logger) {
#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_35
    nix::logger = std::move(new_logger);
#else
    raw_logger_owner() = std::move(new_logger);
    nix::logger = raw_logger_owner().get();
#endif
}

} // namespace nanopynix::nix_compat
