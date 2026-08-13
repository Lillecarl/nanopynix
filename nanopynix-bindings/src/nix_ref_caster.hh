#pragma once
///@file
/// A nanobind type caster for `nix::ref<T>`.
///
/// libstore returns `nix::ref<T>` from several methods, and
/// `Store::queryPathInfo` is the one this repository calls most.
/// `nix::ref` is a `std::shared_ptr` that refuses null.
///
/// **nanobind supports no custom holder.** It ships `std::shared_ptr` and
/// `std::unique_ptr` alone, and it has no equivalent of pybind11's
/// `PYBIND11_DECLARE_HOLDER_TYPE`. Without this caster a binding that returns
/// a `ref` still *compiles*, and it then raises at the first call:
///
///     TypeError: Unable to convert function return value to a Python type!
///     The signature was  query_path_info_typed() -> ref<ValidPathInfo>
///
/// The late failure is the reason this header exists rather than a note.
///
/// **`NB_TYPE_CASTER` does not compile for this type.** That macro declares a
/// plain `Value value;` member, and a `nix::ref` has no default constructor,
/// because a null `ref` cannot exist. The caster below writes the same members
/// by hand and holds the value in an `std::optional`.
///
/// Both directions delegate to the `std::shared_ptr<T>` caster that nanobind
/// ships, which already does the work. This caster adds the null check alone.
/// `T` may be const: `queryPathInfo` returns `ref<const ValidPathInfo>`, and
/// the `std::shared_ptr` caster handles the const itself.

#include <nanobind/nanobind.h>
#include <nanobind/stl/shared_ptr.h>

#include <memory>
#include <optional>
#include <type_traits>
#include <utility>

#include <nix/util/ref.hh>

namespace nanobind::detail {

template<typename T>
struct type_caster<nix::ref<T>>
{
    static constexpr bool IsClass = true;
    using Caster = make_caster<std::shared_ptr<T>>;
    using Value = nix::ref<T>;
    static constexpr auto Name = Caster::Name;

    template<typename T_>
    using Cast = movable_cast_t<T_>;

    template<typename T_>
    static constexpr bool can_cast()
    {
        return true;
    }

    std::optional<Value> value;

    explicit operator Value *()
    {
        return &*value;
    }

    explicit operator Value &()
    {
        return *value;
    }

    explicit operator Value &&()
    {
        return (Value &&) *value;
    }

    bool from_python(handle src, uint8_t flags, cleanup_list * cleanup) noexcept
    {
        Caster caster;
        if (!caster.from_python(src, flags, cleanup))
            return false;
        std::shared_ptr<T> p = caster.operator std::shared_ptr<T> &();
        // The `nix::ref` constructor throws `std::invalid_argument` for a null
        // pointer. Report the failure to nanobind here instead, so the caller
        // sees a `TypeError` and no C++ exception crosses the boundary.
        if (!p)
            return false;
        value.emplace(std::move(p));
        return true;
    }

    static handle from_cpp(const Value & v, rv_policy policy, cleanup_list * cleanup) noexcept
    {
        return Caster::from_cpp(v.get_ptr(), policy, cleanup);
    }

    template<typename T_, enable_if_t<std::is_same_v<std::remove_cv_t<T_>, Value>> = 0>
    static handle from_cpp(T_ * p, rv_policy policy, cleanup_list * list)
    {
        if (!p)
            return none().release();
        return from_cpp(*p, policy, list);
    }
};

} // namespace nanobind::detail
