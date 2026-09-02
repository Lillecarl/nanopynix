#pragma once

#include <memory>
#include <optional>
#include <span>
#include <string>
#include <variant>
#include <vector>

#include <nix/util/logging.hh>

#include <nanopynix/nix_compat_config.hh>

/// The exception specification of the logging methods of `nix::Logger`.
///
/// 2.36 makes each one `noexcept`, because Nix calls them from a completion
/// callback and from a destructor, where it cannot handle an exception. A
/// `PyLogger` method must therefore catch what the Python callback raises: an
/// exception that leaves a `noexcept` function calls `std::terminate`. The
/// methods of `nix_util.cpp` catch on every version, and this macro only
/// keeps the declaration in step with the version that Nix declares.
#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_36
#  define NANOPYNIX_LOGGER_NOEXCEPT
#else
#  define NANOPYNIX_LOGGER_NOEXCEPT noexcept
#endif

namespace nanopynix::nix_compat {

/// The parameter that carries the fields of an activity or of a result.
///
/// 2.36 passes `std::span<const Field>` where 2.35 passes
/// `const std::vector<Field> &`. A span reads a vector without a copy, so a
/// caller on either version writes one loop over this type.
#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_36
using LoggerFields = const nix::Logger::Fields &;
#else
using LoggerFields = std::span<const nix::Logger::Field>;
#endif

/// The integer that a field holds, or nothing when the field holds a string.
///
/// `Field` is a tagged struct with an `enum` in 2.35, and a
/// `std::variant<uint64_t, std::string>` in 2.36.
inline std::optional<uint64_t> field_int(const nix::Logger::Field & f) {
#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_36
    if (f.type == nix::Logger::Field::tInt) return f.i;
    return std::nullopt;
#else
    if (auto * i = std::get_if<uint64_t>(&f)) return *i;
    return std::nullopt;
#endif
}

/// The string that a field holds. Call it only when `field_int` gives nothing.
inline const std::string & field_str(const nix::Logger::Field & f) {
#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_36
    return f.s;
#else
    return std::get<std::string>(f);
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
