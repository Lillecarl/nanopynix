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
#if NANOPYNIX_NIX_LOGGER_IS_UNIQUE_PTR
    return nix::logger.get();
#else
    return nix::logger;
#endif
}

inline void install_logger(std::unique_ptr<nix::Logger> new_logger) {
#if NANOPYNIX_NIX_LOGGER_IS_UNIQUE_PTR
    nix::logger = std::move(new_logger);
#else
    raw_logger_owner() = std::move(new_logger);
    nix::logger = raw_logger_owner().get();
#endif
}

inline void restore_simple_logger() {
    auto new_logger = nix::makeSimpleLogger();
#if NANOPYNIX_NIX_LOGGER_IS_UNIQUE_PTR
    nix::logger = std::move(new_logger);
#else
    raw_logger_owner() = std::move(new_logger);
    nix::logger = raw_logger_owner().get();
#endif
}

} // namespace nanopynix::nix_compat
