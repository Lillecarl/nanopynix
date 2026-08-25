#include <memory>
#include <utility>

#include <nanobind/nanobind.h>

#include "nanopynix_modules.hh"
#include <nanobind/stl/string.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/map.h>
#include <nanobind/stl/vector.h>
#include <nanobind/typing.h>

#include <nix/fetchers/fetchers.hh>
#include <nix/fetchers/fetch-settings.hh>
#include <nix/fetchers/fetch-to-store.hh>
#include <nix/fetchers/attrs.hh>
#include <nix/fetchers/registry.hh>
#include <nix/store/store-api.hh>

#include <nlohmann/json.hpp>

#include <nanopynix/nix_compat_config.hh>

#include "attrs_util.hh"
#include "settings_util.hh"

namespace nb = nanobind;
using namespace nb::literals;

// =========================================================================
// PyInput — wraps nix::fetchers::Input
// =========================================================================

struct PyInput {
    /// The settings that `input` points at.
    ///
    /// Nix 2.31 keeps a `const fetchers::Settings *` inside
    /// `fetchers::Input`, so an input that Python holds must own the settings
    /// it was built against. Before this member the settings were a local of
    /// the function below, and the input pointed at a destroyed object.
    ///
    /// No test on a supported version reaches that pointer through a
    /// `PyInput`, so this half is preventive. `nix_flake.cpp` has the same
    /// defect, and there it fails. See issue #34.
    std::shared_ptr<nix::fetchers::Settings> settings;
    nix::fetchers::Input input;

    PyInput(std::shared_ptr<nix::fetchers::Settings> s, nix::fetchers::Input i)
        : settings(std::move(s)), input(std::move(i)) {}

    std::string to_string() const { return input.to_string(); }
    std::string to_url_string() const { return input.toURLString(); }

    nb::typed<nb::dict, nb::str> to_attrs() const {
        return attrs_to_nb_dict(input.toAttrs());
    }

    std::optional<std::string> get_fingerprint(nix::Store &store) const {
        nb::gil_scoped_release release;
        return input.getFingerprint(store);
    }
};

// =========================================================================
// Free functions
// =========================================================================

static PyInput input_from_url(const std::string &url) {
    // The settings `nix.conf` names, on the heap and owned by the result: see
    // the comment on `PyInput::settings`, and `settings_util.hh` for why the
    // file has to be applied rather than inherited. Issue #234.
    auto settings = std::make_shared<nix::fetchers::Settings>();
    apply_nix_conf(*settings);
    std::optional<nix::fetchers::Input> input;
    {
        nb::gil_scoped_release release;
        input.emplace(nix::fetchers::Input::fromURL(*settings, url));
    }
    return PyInput(std::move(settings), std::move(*input));
}

static PyInput input_from_attrs(const std::map<std::string, std::string> &attrs) {
    nix::fetchers::Attrs a;
    for (auto &[k, v] : attrs) a[k] = v;
    auto settings = std::make_shared<nix::fetchers::Settings>();
    apply_nix_conf(*settings);
    std::optional<nix::fetchers::Input> input;
    {
        nb::gil_scoped_release release;
        input.emplace(nix::fetchers::Input::fromAttrs(*settings, std::move(a)));
    }
    return PyInput(std::move(settings), std::move(*input));
}

// =========================================================================
// The flake registry
// =========================================================================

/// The name that `nix registry list` prints for one registry layer.
///
/// `registry.cc` names the enumerators `Flag`, `User`, `System`, `Global` and
/// `Custom`. `nix registry list` (`src/nix/registry.cc`) prints "flags ",
/// "user  ", "system" and "global" -- the plural and the spaces are column
/// padding for that one command, so this uses the enumerator name in lower
/// case instead.
static const char *registry_type_name(nix::fetchers::Registry::RegistryType type) {
    switch (type) {
    case nix::fetchers::Registry::Flag: return "flag";
    case nix::fetchers::Registry::User: return "user";
    case nix::fetchers::Registry::System: return "system";
    case nix::fetchers::Registry::Global: return "global";
    case nix::fetchers::Registry::Custom: return "custom";
    }
    return "unknown";
}

/// Every entry of every registry Nix would consult, tagged with its layer.
///
/// This is `fetchers::getRegistries` (`libfetchers/registry.cc`), which is
/// what `completeFlakeRef` (`libcmd/installables.cc`) walks to offer a flake
/// reference before the `#`. The four layers come back in the order Nix
/// consults them: flag, user, system, global.
///
/// **The call can download, and it can write a GC root.** The global layer
/// reads the `flake-registry` setting, and its default is a URL. For a URL
/// `getGlobalRegistry` calls `downloadFile`, and then `addPermRoot` on the
/// result under `getCacheDir()/flake-registry.json`. Both are Nix's own
/// behaviour on a Tab press, and the download is TTL-cached, so a warm
/// completion costs nothing. Pass `{"flake-registry": ""}` to drop the layer:
/// that is the value Nix itself reads as "no global registry", and it returns
/// an empty layer without touching the store.
///
/// **Nix caches each layer in a function-local static, so the first call of a
/// process decides for the whole process.** A second call with different
/// settings gets the first call's answer. That is invisible in a command that
/// completes once and exits, and it is not invisible in a long-lived program.
static std::vector<nb::dict> list_registry_entries(
    nix::Store &store,
    const std::map<std::string, std::string> &fetch_settings)
{
    // A fresh settings object, filled from `nix.conf` and then from the
    // caller, who wins: see `settings_util.hh`.
    nix::fetchers::Settings settings;
    apply_nix_conf(settings);
    apply_settings_overrides(settings, fetch_settings);

    nix::fetchers::Registries registries;
    {
        nb::gil_scoped_release release;
        registries = nix::fetchers::getRegistries(settings, store);
    }

    std::vector<nb::dict> result;
    for (auto &registry : registries) {
        for (auto &entry : registry->entries) {
            nb::dict d;
            d["type"] = nb::str(registry_type_name(registry->type));
            // `to_string()`, and not `toURLString()`, because `to_string()` is
            // what `completeFlakeRef` matches a prefix against. The two agree
            // on every entry a registry file can hold; they differ only in the
            // extra query that `nix registry list` composes from
            // `extra_attrs`, which this returns separately.
            d["from"] = nb::str(entry.from.to_string().c_str());
            d["to"] = nb::str(entry.to.to_string().c_str());
            d["exact"] = nb::bool_(entry.exact);
            d["extra_attrs"] = attrs_to_nb_dict(entry.extraAttrs);
            result.push_back(std::move(d));
        }
    }
    return result;
}

// =========================================================================

static void bind_input(nb::module_ &m) {
    // No constructor. A `PyInput` must own the settings that it points at,
    // and Python cannot build the two halves.
    nb::class_<PyInput>(m, "Input")
        .def("to_string", &PyInput::to_string)
        .def("to_url_string", &PyInput::to_url_string)
        .def("to_attrs", &PyInput::to_attrs)
        .def("get_fingerprint", &PyInput::get_fingerprint, "store"_a)
        .def("__str__", &PyInput::to_string)
        .def("__repr__", [](const PyInput &i) {
            return "Input('" + i.to_string() + "')";
        });
}

// =========================================================================

void nanopynix_bind_fetchers(nb::module_ &m) {
    m.doc() = "nanopynix: Nix fetchers bindings (Input, fetch)";

    m.def("input_from_url", &input_from_url, "url"_a,
          "Create an Input from a URL (e.g. 'github:NixOS/nixpkgs')");
    m.def("input_from_attrs", &input_from_attrs, "attrs"_a,
          "Create an Input from a dict of attributes");
    m.def("list_registry_entries", &list_registry_entries, "store"_a,
          "fetch_settings"_a = std::map<std::string, std::string>{},
          "Every flake registry entry Nix would consult, tagged with its layer");
    m.def("list_fetch_settings_metadata_json", []() {
        // Filled from the file as well, so the values this reports are the
        // values a session really gets rather than the compiled defaults.
        nix::fetchers::Settings fetchSettings;
        apply_nix_conf(fetchSettings);
        return fetchSettings.toJSON().dump();
    });

    bind_input(m);
}
