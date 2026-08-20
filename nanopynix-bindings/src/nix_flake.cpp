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
#include <nix/util/hash.hh>

#include <nlohmann/json.hpp>

#include <nanopynix/nix_compat_config.hh>

#include "attrs_util.hh"
#include "settings_util.hh"

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

    PyLockedFlake(
        std::unique_ptr<nix::flake::LockedFlake> lf,
        std::string desc)
        : locked(std::move(lf)), description(std::move(desc))
    {}

    std::string get_description() const { return description; }

    nix::flake::LockedFlake &require_locked() const {
        if (!locked)
            throw std::runtime_error("LockedFlake has been released");
        return *locked;
    }

    /// One node of the lock graph, found the way Nix finds it.
    ///
    /// This is `InstallableFlake::nixpkgsFlakeRef` (`installable-flake.cc`):
    /// `findInput`, then a cast to `LockedNode`. `find_input` is bound rather
    /// than walked in Python because `doFind` (`lockfile.cc`) is not a plain
    /// lookup. It resolves a `follows` edge by recursion from the root, and it
    /// raises on a follow cycle. A walk over the serialised graph would have to
    /// derive both again.
    ///
    /// Returns nothing when the path names no input, and also when it names the
    /// root, which is a `Node` and not a `LockedNode`. The root carries no
    /// locked reference, so there is nothing to report about it.
    std::optional<nb::typed<nb::dict, nb::str, nb::object>>
    find_input(const std::vector<std::string> &path) const {
        auto &lf = require_locked();
        std::shared_ptr<const nix::flake::LockedNode> node;
        {
            nb::gil_scoped_release release;
            nix::flake::InputAttrPath attrPath(path.begin(), path.end());
            node = std::dynamic_pointer_cast<const nix::flake::LockedNode>(
                lf.lockFile.findInput(attrPath));
        }
        if (!node)
            return std::nullopt;
        nb::dict result;
        result["locked_ref"] = nb::str(node->lockedRef.to_string().c_str());
        result["original_ref"] = nb::str(node->originalRef.to_string().c_str());
        result["is_flake"] = nb::bool_(node->isFlake);
        return result;
    }

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
#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_35
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

static PyFlakeRef parse_flake_ref(const std::string &url,
                                   const std::map<std::string, std::string> &fetch_settings = {}) {
    // On the heap and owned by the result: see the comment on
    // `PyFlakeRef::settings`.
    auto settings = std::make_shared<nix::fetchers::Settings>();
    apply_settings_overrides(*settings, fetch_settings);
    std::optional<nix::FlakeRef> ref;
    {
        nb::gil_scoped_release release;
        // The base directory is the working directory, as `eval_flake` below
        // already passes and as the Nix command line uses.
        //
        // It is not optional. `parsePathFlakeRefWithFragment` (`flakeref.cc`)
        // keeps its whole path branch inside `if (baseDir)`: the search upward
        // for a `.git` directory, and the rewrite of the reference to
        // `git+file://`. Without a base directory a path always parses as
        // `path:`, so a git working tree that Nix calls `git+file:///w` was
        // called `path:/w` here, and `lock_flake` disagreed with `eval_flake`
        // about the same string. A relative path did not parse at all -- the
        // `else` branch of that function rejects one.
        ref.emplace(nix::parseFlakeRef(*settings, url, std::filesystem::current_path()));
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
        auto path = nix::flake::NonEmptyInputAttrPath::parse(input);
        if (!path)
            throw std::runtime_error(
                "input path cannot be empty: '" + input + "'");
        lockFlags.inputUpdates.insert(*path);
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

    return PyLockedFlake(std::move(locked), std::move(desc));
}

/// The object that `nix flake metadata --json` prints.
///
/// This is `CmdFlakeMetadata::run` (`src/nix/flake.cc`), copied line for line.
/// Nix builds the whole object from one `LockedFlake` and one `Store`, so this
/// binding does too. The alternative was to bind `getFingerprint`,
/// `toStorePath`, `getRev`, `getRevCount`, `getLastModified` and the three
/// reference accessors on their own, and to assemble the object again in
/// Python. Nothing is assembled here, so there is no faithfulness question to
/// prove.
///
/// The store is the build store when there is one, which is the rule in
/// `nix_expr.cpp`. Nix uses the store of the command, and not the evaluation
/// store.
static std::string metadata_json(PyEvalState &es, PyLockedFlake &lf) {
    es.checkThread();
    auto &lockedFlake = lf.require_locked();
    auto &flake = lockedFlake.flake;
    auto store = es.build_store ? es.build_store : es.store;
    if (!store)
        throw std::runtime_error("the evaluator has no store");

    std::string out;
    // A block, and not a release for the whole function: `out` is converted to
    // a Python string after this returns, and the GIL is held again by then
    // either way. Every other release in this file is scoped the same, so the
    // rule is one rule.
    {
        nb::gil_scoped_release release;

        nlohmann::json j;
        if (flake.description)
            j["description"] = *flake.description;
        j["originalUrl"] = flake.originalRef.to_string();
        j["original"] = nix::fetchers::attrsToJSON(flake.originalRef.toAttrs());
        j["resolvedUrl"] = flake.resolvedRef.to_string();
        j["resolved"] = nix::fetchers::attrsToJSON(flake.resolvedRef.toAttrs());
        j["url"] = flake.lockedRef.to_string();
        j["locked"] = nix::fetchers::attrsToJSON(flake.lockedRef.toAttrs());
        if (auto rev = flake.lockedRef.input.getRev())
            j["revision"] = rev->to_string(nix::HashFormat::Base16, false);
        if (auto dirtyRev = nix::fetchers::maybeGetStrAttr(flake.lockedRef.toAttrs(), "dirtyRev"))
            j["dirtyRevision"] = *dirtyRev;
        if (auto revCount = flake.lockedRef.input.getRevCount())
            j["revCount"] = *revCount;
        if (auto lastModified = flake.lockedRef.input.getLastModified())
            j["lastModified"] = *lastModified;
        // Nix 2.35 splits these two calls across the assignment and the JSON line.
        // The expression is the same in every supported version, so one line covers
        // all three.
        j["path"] = store->printStorePath(store->toStorePath(flake.path.path.abs()).first);
        j["locks"] = lockedFlake.lockFile.toJSON().first;
        // No version difference is left in the whole lock-file surface:
        // `InputAttrPath`, `Node::Edge`, `findInput`, `toJSON` and
        // `getFingerprint` are identical in Nix 2.34 and 2.35. `getFingerprint`
        // took a `ref<Store>` below the supported floor.
        auto fingerprint = lockedFlake.getFingerprint(*store, es.fetchSettings);
        if (fingerprint)
            j["fingerprint"] = fingerprint->to_string(nix::HashFormat::Base16, false);

        out = j.dump();
    }
    return out;
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
        .def("find_input", &PyLockedFlake::find_input, "path"_a,
             "Find one node of the lock graph, as InstallableFlake::nixpkgsFlakeRef does")
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
          "Lock a flake reference, returning a LockedFlake");
    m.def("get_flake", &get_flake,
          "state"_a, "flake_ref"_a, "use_registries"_a = true,
          "Resolve a flake reference (without locking)");
    m.def("call_flake", &call_flake,
          "state"_a, "locked_flake"_a,
          nb::keep_alive<0, 1>(),
          "Call a locked flake's outputs function, returning a Value");
    m.def("metadata_json", &metadata_json,
          "state"_a, "locked_flake"_a,
          "The JSON object that `nix flake metadata --json` prints");
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
