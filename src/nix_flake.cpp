#include <nanobind/nanobind.h>
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

#include "attrs_util.hh"

#include "py_eval.hh"

namespace nb = nanobind;
using namespace nb::literals;

// =========================================================================
// PyFlakeRef
// =========================================================================

struct PyFlakeRef {
    nix::FlakeRef ref;

    PyFlakeRef(nix::FlakeRef r) : ref(std::move(r)) {}

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
};

// =========================================================================
// Free functions
// =========================================================================

static PyFlakeRef parse_flake_ref(const std::string &url) {
    nix::flake::Settings flakeSettings;
    nix::fetchers::Settings fetchSettings;
    auto ref = nix::parseFlakeRef(fetchSettings, url);
    return PyFlakeRef(std::move(ref));
}

static PyLockedFlake lock_flake(PyEvalState &es, PyFlakeRef &flakeRef,
                                 bool updateLockFile = true,
                                 bool writeLockFile = true) {
    nix::flake::Settings flakeSettings;
    nix::flake::LockFlags lockFlags;
    lockFlags.updateLockFile = updateLockFile;
    lockFlags.writeLockFile = writeLockFile;

    auto locked = std::make_unique<nix::flake::LockedFlake>(
        nix::flake::lockFlake(flakeSettings, *es.state, flakeRef.ref, lockFlags));

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
    auto flake = nix::flake::getFlake(
        *es.state, flakeRef.ref,
        useRegistries ? nix::fetchers::UseRegistries::All
                       : nix::fetchers::UseRegistries::No);
    return PyFlakeRef(std::move(flake.resolvedRef));
}

// =========================================================================
// bindings
// =========================================================================

static void bind_flake_ref(nb::module_ &m) {
    nb::class_<PyFlakeRef>(m, "FlakeRef")
        .def(nb::init<nix::FlakeRef>(), "ref"_a)
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
        .def("__repr__", [](const PyLockedFlake &lf) {
            return "LockedFlake(description='" + lf.description + "')";
        });
}

// =========================================================================

NB_MODULE(nanopynix_flake, m) {
    m.doc() = "nanopynix: Nix flake bindings (FlakeRef, lockFlake)";

    m.def("parse_flake_ref", &parse_flake_ref, "url"_a,
          "Parse a flake reference string (e.g. 'github:NixOS/nixpkgs')");
    m.def("lock_flake", &lock_flake,
          "state"_a, "flake_ref"_a,
          "update_lock_file"_a = true, "write_lock_file"_a = true,
          "Lock a flake reference, returning a LockedFlake with description and inputs");
    m.def("get_flake", &get_flake,
          "state"_a, "flake_ref"_a, "use_registries"_a = true,
          "Resolve a flake reference (without locking)");

    bind_flake_ref(m);
    bind_locked_flake(m);
}
