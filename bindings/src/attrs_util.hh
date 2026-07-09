#pragma once

#include <nanobind/nanobind.h>
#include <nanobind/typing.h>

#include <nix/fetchers/attrs.hh>

namespace nb = nanobind;

/// Convert nix::fetchers::Attrs (map<string, variant<string, uint64_t, Explicit<bool>>>)
/// to an nb::dict for Python consumption.
inline nb::dict attrs_to_nb_dict(const nix::fetchers::Attrs &attrs) {
    nb::dict d;
    for (auto &[k, v] : attrs) {
        std::visit([&](auto &&val) {
            using T = std::decay_t<decltype(val)>;
            if constexpr (std::is_same_v<T, std::string>)
                d[nb::str(k.c_str())] = nb::str(val.c_str());
            else if constexpr (std::is_same_v<T, uint64_t>)
                d[nb::str(k.c_str())] = nb::int_(val);
            else if constexpr (std::is_same_v<T, nix::Explicit<bool>>)
                d[nb::str(k.c_str())] = nb::bool_(val.t);
        }, v);
    }
    return d;
}
