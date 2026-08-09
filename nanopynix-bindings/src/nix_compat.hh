#pragma once

#include <memory>
#include <string>

#include <nanopynix/nix_compat_config.hh>

#if NANOPYNIX_NIX_VERSION_NUMBER >= NANOPYNIX_NIX_2_36
#  include <span>
#  include <variant>
#endif

#include <nix/util/logging.hh>

// Nix 2.36 changed three things about `nix::Logger` at once.
//
// 1. Every logging method is `noexcept`. Nix calls a logger from a place that
//    cannot handle an exception, so an escaping exception calls
//    `std::terminate`. An override must repeat the `noexcept`, or the compiler
//    rejects it as a looser exception specification.
// 2. `Logger::Fields`, which was a `std::vector<Field>`, is now
//    `std::span<const Field>` at each call site. The alias is gone.
// 3. `Logger::Field` is a `std::variant<uint64_t, std::string>`, and not a
//    struct that holds a `type` tag beside an `i` member and an `s` member.
//
// `NANOPYNIX_LOG_NOEXCEPT` and `nanopynix::nix_compat::LogFields` carry the
// first two changes. `log_field_is_int`, `log_field_int` and
// `log_field_string` carry the third one.
#if NANOPYNIX_NIX_VERSION_NUMBER >= NANOPYNIX_NIX_2_36
#  define NANOPYNIX_LOG_NOEXCEPT noexcept
#else
#  define NANOPYNIX_LOG_NOEXCEPT
#endif

namespace nanopynix::nix_compat {

/// The parameter type of `startActivity` and of `result`.
///
/// It is a reference before 2.36 and a span by value from 2.36. An override
/// must repeat the type of the base exactly, so this alias is the whole
/// parameter type, and not the type that the parameter refers to.
#if NANOPYNIX_NIX_VERSION_NUMBER >= NANOPYNIX_NIX_2_36
using LogFieldsParam = std::span<const nix::Logger::Field>;
#else
using LogFieldsParam = const nix::Logger::Fields &;
#endif

inline bool log_field_is_int(const nix::Logger::Field & f) {
#if NANOPYNIX_NIX_VERSION_NUMBER >= NANOPYNIX_NIX_2_36
    return std::holds_alternative<uint64_t>(f);
#else
    return f.type == nix::Logger::Field::tInt;
#endif
}

inline uint64_t log_field_int(const nix::Logger::Field & f) {
#if NANOPYNIX_NIX_VERSION_NUMBER >= NANOPYNIX_NIX_2_36
    return std::get<uint64_t>(f);
#else
    return f.i;
#endif
}

inline const std::string & log_field_string(const nix::Logger::Field & f) {
#if NANOPYNIX_NIX_VERSION_NUMBER >= NANOPYNIX_NIX_2_36
    return std::get<std::string>(f);
#else
    return f.s;
#endif
}


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
