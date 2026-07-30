#include <filesystem>
#include <memory>
#include <utility>

#include <nanobind/nanobind.h>

#include "nanopynix_modules.hh"
#include <nanobind/stl/string.h>
#include <nanobind/stl/vector.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/map.h>
#include <nanobind/typing.h>

#include <nix/flake/flake.hh>
#include <nix/flake/flakeref.hh>
#include <nix/flake/lockfile.hh>
#include <nix/flake/settings.hh>
#include <nix/fetchers/fetchers.hh>
#include <nix/store/store-api.hh>
#include <nix/util/configuration.hh>

#include <nlohmann/json.hpp>

#include <nanopynix/nix_compat_config.hh>

#include "attrs_util.hh"

#include "py_value.hh"

namespace nb = nanobind;
using namespace nb::literals;

// =========================================================================
// PyFlakeRef
// =========================================================================

struct PyFlakeRef {
    /// The settings that `ref.input` points at.
    ///
    /// Nix 2.31 keeps a `const fetchers::Settings *` inside `fetchers::Input`,
    /// and its git fetcher reads through that pointer long after the parse.
    /// So a flake reference that Python holds must own the settings it was
    /// parsed against. Before this member the settings were a local of
    /// `parse_flake_ref`, and every `lock_flake` on 2.31 read freed memory.
    /// See issue #34.
    ///
    /// A `shared_ptr`, because a `PyFlakeRef` is copied and moved while the
    /// raw pointer inside `ref` stays as it was.
    std::shared_ptr<nix::fetchers::Settings> settings;
    nix::FlakeRef ref;

    PyFlakeRef(std::shared_ptr<nix::fetchers::Settings> s, nix::FlakeRef r)
        : settings(std::move(s)), ref(std::move(r)) {}

    std::string to_string() const { return ref.to_string(); }

    nb::dict to_attrs() const {
        return attrs_to_nb_dict(ref.toAttrs());
    }
};

// =========================================================================
// PyLockedFlake
// =========================================================================

struct PyLockedFlake {
    std::unique_ptr<nix::flake::LockedFlake> locked;
    std::string description;
    nb::dict inputs;

    PyLockedFlake(
        std::unique_ptr<nix::flake::LockedFlake> lf,
        std::string desc,
        nb::dict inps)
        : locked(std::move(lf)), description(std::move(desc)), inputs(std::move(inps))
    {}

    std::string get_description() const { return description; }
    nb::typed<nb::dict, nb::str> get_inputs() const { return inputs; }

    void write_lock_file() const {
        if (!locked)
            throw std::runtime_error("LockedFlake has been released");
        {
            nb::gil_scoped_release release;
            auto [lockFileStr, keyMap] = locked->lockFile.to_string();
            auto relPath = (locked->flake.originalRef.subdir == "" ? "" : locked->flake.originalRef.subdir + "/") + "flake.lock";
            locked->flake.originalRef.input.putFile(
                nix::CanonPath(relPath),
                lockFileStr + "\n",
                std::nullopt);
#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_32
            // SourcePath did not expose a cache invalidation hook yet.
#elif NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_35
            locked->flake.lockFilePath().invalidateCache();
#else
            // SourcePath cache invalidation was removed in Nix 2.35.
#endif
        }
    }
};

// =========================================================================
// Free functions
// =========================================================================

static void apply_settings_overrides(nix::Config &config, const std::map<std::string, std::string> &overrides) {
    for (auto &[name, value] : overrides)
        if (!config.set(name, value))
            throw std::runtime_error("unknown setting: " + name);
}

static PyFlakeRef parse_flake_ref(const std::string &url,
                                   const std::map<std::string, std::string> &fetch_settings = {}) {
    // On the heap and owned by the result: see the comment on
    // `PyFlakeRef::settings`.
    auto settings = std::make_shared<nix::fetchers::Settings>();
    apply_settings_overrides(*settings, fetch_settings);
    std::optional<nix::FlakeRef> ref;
    {
        nb::gil_scoped_release release;
        ref.emplace(nix::parseFlakeRef(*settings, url));
    }
    return PyFlakeRef(std::move(settings), std::move(*ref));
}

/// Re-parse `flakeRef` against the fetch settings of `es`.
///
/// Nix 2.34 and Nix 2.35 pass `state.fetchSettings` to each fetcher call, so
/// an evaluator's own fetch settings already decide there. Nix 2.31 reads the
/// settings that the reference was parsed against instead. This makes the
/// three versions agree: the evaluator owns the fetch scope, and a setting
/// baked into a parsed reference applies to the parse alone.
///
/// `es.fetchSettings` outlives `es.state`, which Nix requires of it, so the
/// result is valid for as long as the evaluator is.
static nix::FlakeRef bind_to_evaluator(PyEvalState &es, const PyFlakeRef &flakeRef) {
    return nix::FlakeRef::fromAttrs(es.fetchSettings, flakeRef.ref.toAttrs());
}

static PyLockedFlake lock_flake(
    PyEvalState &es,
    PyFlakeRef &flakeRef,
    nb::object update_inputs = nb::bool_(false),
    bool write_lock_file = true,
    const std::map<std::string, std::string> &flake_settings = {})
{
    es.checkThread();
    nix::flake::Settings flakeSettings;
    apply_settings_overrides(flakeSettings, flake_settings);
    nix::flake::LockFlags lockFlags;
    lockFlags.writeLockFile = write_lock_file;

    std::vector<std::string> input_updates;
    if (nb::isinstance<nb::bool_>(update_inputs)) {
        lockFlags.recreateLockFile = nb::cast<bool>(update_inputs);
    } else if (nb::isinstance<nb::list>(update_inputs)) {
        input_updates = nb::cast<std::vector<std::string>>(update_inputs);
    } else {
        throw std::runtime_error("update_inputs must be a bool or list[str]");
    }

    for (const auto &input : input_updates) {
#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_32
        if (input.empty())
            throw std::runtime_error("input path cannot be empty: '" + input + "'");
        lockFlags.inputUpdates.insert(nix::flake::parseInputAttrPath(input));
#else
        auto path = nix::flake::NonEmptyInputAttrPath::parse(input);
        if (!path)
            throw std::runtime_error(
                "input path cannot be empty: '" + input + "'");
        lockFlags.inputUpdates.insert(*path);
#endif
    }

    std::unique_ptr<nix::flake::LockedFlake> locked;
    {
        nb::gil_scoped_release release;
        // The result points into `es.fetchSettings`, which is why the binding
        // keeps the evaluator alive.
        locked = std::make_unique<nix::flake::LockedFlake>(
            nix::flake::lockFlake(flakeSettings, *es.state,
                                  bind_to_evaluator(es, flakeRef), lockFlags));
    }

    std::string desc;
    if (locked->flake.description)
        desc = *locked->flake.description;

    nb::dict inputs;
    for (auto &[id, input] : locked->flake.inputs) {
        nb::dict inp;
        if (input.ref) {
            inp["ref"] = nb::str(input.ref->to_string().c_str());
            inp["is_flake"] = nb::bool_(input.isFlake);
        }
        if (input.follows) {
            nb::list follows;
            for (auto &f : *input.follows)
                follows.append(nb::str(f.c_str()));
            inp["follows"] = follows;
        }
        inputs[nb::str(id.c_str())] = inp;
    }

    return PyLockedFlake(std::move(locked), std::move(desc), std::move(inputs));
}

static PyFlakeRef get_flake(PyEvalState &es, PyFlakeRef &flakeRef,
                             bool useRegistries = true) {
    es.checkThread();
    std::optional<nix::flake::Flake> flake;
    nix::fetchers::Attrs resolved;
    {
        nb::gil_scoped_release release;
        flake.emplace(nix::flake::getFlake(
            *es.state, bind_to_evaluator(es, flakeRef),
            useRegistries ? nix::fetchers::UseRegistries::All
                          : nix::fetchers::UseRegistries::No));
        resolved = flake->resolvedRef.toAttrs();
    }
    // Back onto this reference's own settings, so the result is
    // self-contained. Every `PyFlakeRef` owns what its `Input` points at, and
    // no caller has to know which evaluator resolved it.
    return PyFlakeRef(flakeRef.settings,
                      nix::FlakeRef::fromAttrs(*flakeRef.settings, resolved));
}

static PyValue call_flake(PyEvalState &es, PyLockedFlake &lf) {
    es.checkThread();
    nix::Value *v;
    {
        nb::gil_scoped_release release;
        v = es.state->allocValue();
        nix::flake::callFlake(*es.state, *lf.locked, *v);
    }
    return PyValue(v, &es, es.alive);
}

static PyValue eval_flake(PyEvalState &es, const std::string &ref,
                           bool write_lock_file = true,
                           const std::map<std::string, std::string> &flake_settings = {}) {
    es.checkThread();
    nix::flake::Settings flakeSettings;
    apply_settings_overrides(flakeSettings, flake_settings);
    nix::flake::LockFlags lockFlags;
    lockFlags.writeLockFile = write_lock_file;
    nix::Value *v;
    {
        nb::gil_scoped_release release;
        auto flakeRef = nix::parseFlakeRef(
            es.fetchSettings, ref, std::filesystem::current_path());
        auto lockedFlake = nix::flake::lockFlake(
            flakeSettings, *es.state, flakeRef, lockFlags);
        v = es.state->allocValue();
        nix::flake::callFlake(*es.state, lockedFlake, *v);
    }

    return PyValue(v, &es, es.alive);
}

// =========================================================================
// bindings
// =========================================================================

static void bind_flake_ref(nb::module_ &m) {
    // No constructor. A `PyFlakeRef` must own the settings that its `Input`
    // points at, and Python cannot build the two halves. `parse_flake_ref` is
    // the one door.
    nb::class_<PyFlakeRef>(m, "FlakeRef")
        .def("to_string", &PyFlakeRef::to_string)
        .def("to_attrs", &PyFlakeRef::to_attrs)
        .def("__str__", &PyFlakeRef::to_string)
        .def("__repr__", [](const PyFlakeRef &r) {
            return "FlakeRef('" + r.to_string() + "')";
        });
}

static void bind_locked_flake(nb::module_ &m) {
    nb::class_<PyLockedFlake>(m, "LockedFlake")
        .def("description", &PyLockedFlake::get_description)
        .def("inputs", &PyLockedFlake::get_inputs)
        .def("write_lock_file", &PyLockedFlake::write_lock_file,
             "Write the in-memory lock file to the flake's flake.lock on disk")
        .def("__repr__", [](const PyLockedFlake &lf) {
            return "LockedFlake(description='" + lf.description + "')";
        });
}

// =========================================================================

void nanopynix_bind_flake(nb::module_ &m) {
    m.doc() = "nanopynix: Nix flake bindings (FlakeRef, lockFlake, callFlake)";

    // Register builtins.getFlake on every EvalState by configuring its
    // EvalSettings with flake primops.
    PyEvalState::evalSettingsConfigurators().push_back(
        [](nix::EvalSettings &es) {
            static nix::flake::Settings flakeSettings;
            flakeSettings.configureEvalSettings(es);
        });

    m.def("parse_flake_ref", &parse_flake_ref, "url"_a,
          "fetch_settings"_a = std::map<std::string, std::string>{},
          "Parse a flake reference string (e.g. 'github:NixOS/nixpkgs')");
    m.def("lock_flake", &lock_flake,
          "state"_a, "flake_ref"_a,
          "update_inputs"_a = nb::bool_(false),
          "write_lock_file"_a = true,
          "flake_settings"_a = std::map<std::string, std::string>{},
          // The LockedFlake points into the evaluator's fetch settings, and
          // into the arena that its SourcePaths come from.
          nb::keep_alive<0, 1>(),
          "Lock a flake reference, returning a LockedFlake with description and inputs");
    m.def("get_flake", &get_flake,
          "state"_a, "flake_ref"_a, "use_registries"_a = true,
          "Resolve a flake reference (without locking)");
    m.def("call_flake", &call_flake,
          "state"_a, "locked_flake"_a,
          nb::keep_alive<0, 1>(),
          "Call a locked flake's outputs function, returning a Value");
    m.def("eval_flake", &eval_flake,
          "state"_a, "ref"_a,
          "write_lock_file"_a = true,
          "flake_settings"_a = std::map<std::string, std::string>{},
          nb::keep_alive<0, 1>(),
          "Lock and evaluate a flake, returning its outputs as a Value");
    m.def("list_flake_settings_metadata_json", []() {
        nix::flake::Settings flakeSettings;
        return flakeSettings.toJSON().dump();
    });

    bind_flake_ref(m);
    bind_locked_flake(m);
}
