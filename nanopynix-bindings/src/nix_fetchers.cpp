#include <filesystem>
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
#include <nix/flake/flakeref.hh>
#include <nix/store/store-api.hh>
#include <nix/util/source-accessor.hh>
#include <nix/util/source-path.hh>

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

/// One registry file, read from disk and not from Nix's cache.
///
/// **This is deliberately not `getUserRegistry` or `getCustomRegistry`.**
/// Both of those keep the answer in a function-local static, so the first
/// call of a process decides for the whole process. `nix registry add` can
/// live with that, because the command writes the file and exits. A
/// long-lived program cannot: a second write would build on the first read,
/// and a test that gives each case its own registry file would read the file
/// of the case before it. `Registry::read` is a plain static member, it takes
/// the path, and it reads the file every time.
///
/// The type is `User` because that is the layer these operations write.
/// Nothing else reads the field on the way back out.
static std::shared_ptr<nix::fetchers::Registry>
read_registry_at(const nix::fetchers::Settings &settings, const std::filesystem::path &path)
{
    return nix::fetchers::Registry::read(
        settings,
        nix::SourcePath{nix::getFSSourceAccessor(), nix::CanonPath{path.string()}}.resolveSymlinks(),
        nix::fetchers::Registry::User);
}

/// The path a write goes to: what the caller named, or the user registry.
static std::filesystem::path registry_path_or_user(const std::string &path) {
    return path.empty() ? nix::fetchers::getUserRegistryPath() : std::filesystem::path(path);
}

/// What one change to a registry file did, as `pynix registry` reports it.
static nb::dict registry_write_result(
    const std::filesystem::path &path,
    size_t removed,
    std::optional<std::string> to,
    std::optional<bool> locked)
{
    nb::dict d;
    d["path"] = nb::str(path.string().c_str());
    d["removed"] = nb::int_(static_cast<uint64_t>(removed));
    d["to"] = to ? nb::object(nb::str(to->c_str())) : nb::object(nb::none());
    d["locked"] = locked ? nb::object(nb::bool_(*locked)) : nb::object(nb::none());
    return d;
}

/// `nix registry add`: replace what `from_url` resolves to with `to_url`.
///
/// This is `CmdRegistryAdd::run` (`src/nix/registry.cc`) with the path made
/// explicit. Both halves parse as flake references, and not as inputs,
/// because a flake reference carries the subdirectory. Nix stores that
/// subdirectory as the `dir` extra attribute rather than as a part of the
/// target, and `Input::fromURL` would drop it.
///
/// The remove before the add is Nix's own: an entry replaces a previous entry
/// for the same `from`, and it does not join it.
static nb::dict registry_add(
    const std::string &path,
    const std::string &from_url,
    const std::string &to_url,
    const std::map<std::string, std::string> &fetch_settings)
{
    // A fresh settings object, filled from `nix.conf` and then from the
    // caller, who wins: see `settings_util.hh`. It is a local, and every
    // `Input` that points at it dies with this call.
    nix::fetchers::Settings settings;
    apply_nix_conf(settings);
    apply_settings_overrides(settings, fetch_settings);

    auto file = registry_path_or_user(path);
    size_t removed = 0;
    std::string to;
    {
        nb::gil_scoped_release release;
        // The working directory, as `parse_flake_ref` in `nix_flake.cpp`
        // passes and as the Nix command line uses. That file gives the whole
        // reason a base directory is not optional.
        auto base = std::filesystem::current_path();
        auto fromRef = nix::parseFlakeRef(settings, from_url, base);
        auto toRef = nix::parseFlakeRef(settings, to_url, base);
        auto registry = read_registry_at(settings, file);
        nix::fetchers::Attrs extraAttrs;
        if (toRef.subdir != "")
            extraAttrs["dir"] = toRef.subdir;
        auto before = registry->entries.size();
        registry->remove(fromRef.input);
        removed = before - registry->entries.size();
        registry->add(fromRef.input, toRef.input, extraAttrs);
        registry->write(file);
        to = toRef.input.to_string();
    }
    return registry_write_result(file, removed, to, std::nullopt);
}

/// `nix registry remove`: drop every entry whose `from` is `from_url`.
///
/// `Registry::remove` compares whole inputs, so `flake:nixpkgs` removes an
/// entry written as `nixpkgs` and does not remove one written as
/// `nixpkgs/nixos-unstable`. The count comes back because Nix's own command
/// says nothing when it removes nothing, and a caller cannot tell the two
/// apart from the file alone.
static nb::dict registry_remove(
    const std::string &path,
    const std::string &from_url,
    const std::map<std::string, std::string> &fetch_settings)
{
    nix::fetchers::Settings settings;
    apply_nix_conf(settings);
    apply_settings_overrides(settings, fetch_settings);

    auto file = registry_path_or_user(path);
    size_t removed = 0;
    {
        nb::gil_scoped_release release;
        auto ref = nix::parseFlakeRef(settings, from_url, std::filesystem::current_path());
        auto registry = read_registry_at(settings, file);
        auto before = registry->entries.size();
        registry->remove(ref.input);
        removed = before - registry->entries.size();
        registry->write(file);
    }
    return registry_write_result(file, removed, std::nullopt, std::nullopt);
}

/// `nix registry pin`: point `url` at what `locked_url` resolves to now.
///
/// This is `CmdRegistryPin::run` (`src/nix/registry.cc`). It resolves through
/// the registry, then fetches, and the fetch is what turns a branch into a
/// revision. An empty `locked_url` pins the reference to itself, which is
/// what the command does with one argument.
///
/// **The store is not optional here, and the call reaches the network.**
/// `getAccessor` fetches the flake. That is the whole point: an unfetched
/// reference has no revision to pin to.
///
/// `locked` reports what Nix warns about. `Input::isLocked` is false for a
/// reference that carries no revision, and pinning such a reference writes an
/// entry that still moves.
static nb::dict registry_pin(
    nix::Store &store,
    const std::string &path,
    const std::string &url,
    const std::string &locked_url,
    const std::map<std::string, std::string> &fetch_settings)
{
    nix::fetchers::Settings settings;
    apply_nix_conf(settings);
    apply_settings_overrides(settings, fetch_settings);

    auto file = registry_path_or_user(path);
    size_t removed = 0;
    std::string to;
    bool isLocked = false;
    {
        nb::gil_scoped_release release;
        auto base = std::filesystem::current_path();
        auto ref = nix::parseFlakeRef(settings, url, base);
        auto lockedRef = nix::parseFlakeRef(settings, locked_url.empty() ? url : locked_url, base);
        auto resolvedInput = lockedRef.resolve(settings, store).input;
        auto resolved = resolvedInput.getAccessor(settings, store).second;
        isLocked = resolved.isLocked(settings);
        auto registry = read_registry_at(settings, file);
        nix::fetchers::Attrs extraAttrs;
        if (ref.subdir != "")
            extraAttrs["dir"] = ref.subdir;
        auto before = registry->entries.size();
        registry->remove(ref.input);
        removed = before - registry->entries.size();
        registry->add(ref.input, resolved, extraAttrs);
        registry->write(file);
        to = resolved.to_string();
    }
    return registry_write_result(file, removed, to, isLocked);
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
    m.def("user_registry_path", []() {
        return nix::fetchers::getUserRegistryPath().string();
    }, "The registry file of the user, which is what a write defaults to");
    m.def("registry_add", &registry_add, "path"_a, "from_url"_a, "to_url"_a,
          "fetch_settings"_a = std::map<std::string, std::string>{},
          "Point a flake reference at another one, in one registry file");
    m.def("registry_remove", &registry_remove, "path"_a, "from_url"_a,
          "fetch_settings"_a = std::map<std::string, std::string>{},
          "Drop every entry for a flake reference, from one registry file");
    m.def("registry_pin", &registry_pin, "store"_a, "path"_a, "url"_a,
          "locked_url"_a = std::string{},
          "fetch_settings"_a = std::map<std::string, std::string>{},
          "Pin a flake reference to the reference it resolves to now");
    m.def("list_fetch_settings_metadata_json", []() {
        // Filled from the file as well, so the values this reports are the
        // values a session really gets rather than the compiled defaults.
        nix::fetchers::Settings fetchSettings;
        apply_nix_conf(fetchSettings);
        return fetchSettings.toJSON().dump();
    });

    bind_input(m);
}
