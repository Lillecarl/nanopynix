/// The one and only C++ -> Python exception translator for Nix errors.
///
/// Every Nix exception class and the single translator that dispatches them
/// live here together, deliberately. nanobind keeps translators in a global
/// append-to-front list walked head-first, so with one translator *per type*
/// the registration order decides which one wins -- and that order is the
/// order the extension modules happen to be imported in, which no translation
/// unit can see or enforce. That fragility is why `nix::Error` itself used to
/// be left unregistered (it is the base of every bound type, so registering it
/// anywhere risked shadowing all of them), and leaving it unregistered is
/// exactly why an error Nix threw as a plain `nix::Error` reached Python as a
/// bare `RuntimeError` with its `ErrorInfo` -- position, trace, suggestions --
/// thrown away.
///
/// With a single translator there is nothing left to order: it owns the whole
/// hierarchy and dispatches internally through an ordered `catch` chain.
/// C++ tries `catch` clauses in source order, so the chain must run
/// most-derived first -- and unlike nanobind's registration order, getting
/// that wrong is a *compile-time* error rather than a silent runtime surprise:
///
///     warning: exception of type 'X' will be caught by earlier handler
///     [-Wexceptions]
///
/// so the ordering rule below is enforced by the compiler, not by convention.
///
/// Three things this must NOT do, in decreasing subtlety:
///
/// 1. **Do not root the chain at `nix::BaseError`.** `BaseError`, not `Error`,
///    is what derives from `std::exception`, and `nix::Interrupted` hangs off
///    `BaseError` *beside* `Error` rather than under it. Nix says so itself
///    (`nix/util/error.hh`): "BaseError should generally not be caught, as it
///    has Interrupted as a subclass. Catch Error instead." Rooting here would
///    quietly turn a Ctrl-C into a `NixError`.
/// 2. **Do not catch standard exceptions.** Nix throws plenty that are not
///    `nix::BaseError` at all -- `std::bad_alloc`, nlohmann-json errors,
///    `std::filesystem::filesystem_error`. Declining them is the entire
///    implementation: an exception this lambda does not catch propagates out,
///    nanobind resumes walking its list, and reaches
///    `default_exception_translator`, which is registered at internals init
///    with `next = nullptr` and so is permanently the tail. It already maps
///    that family well (bad_alloc -> MemoryError, out_of_range -> IndexError,
///    overflow_error -> OverflowError, ... , std::exception -> RuntimeError),
///    which is also how our own `std::out_of_range` in `list_get` reaches
///    Python as `IndexError`. Silence here is the feature.
/// 3. **Do not add a `catch (...)`.** It would defeat (2).

#include <nanobind/nanobind.h>

#include <nix/expr/eval-error.hh>
#include <nix/store/store-api.hh>
#include <nix/util/error.hh>
#include <nix/util/signals.hh>

#include "nanopynix_errors.hh"
#include "nix_error_info.hh"

namespace nb = nanobind;

namespace {

/// Python type objects for the bound Nix exception classes.
///
/// File-static rather than shared across modules on purpose: the extension
/// modules are separately linked shared objects built with hidden visibility,
/// so a template-static in a header would NOT be merged between them and each
/// `.so` would silently see its own null copy. Keeping the classes and the
/// translator in one translation unit sidesteps that entirely.
struct PyErrorTypes {
    PyObject *error = nullptr; // nix::Error, the catch-all
    PyObject *eval_base_error = nullptr;
    PyObject *eval_error = nullptr;
    PyObject *parse_error = nullptr;
    PyObject *type_error = nullptr;
    PyObject *undefined_var_error = nullptr;
    PyObject *assertion_error = nullptr;
    PyObject *thrown_error = nullptr;
    PyObject *invalid_path = nullptr;
    PyObject *unsupported = nullptr;
    PyObject *bad_store_path = nullptr;
    PyObject *sys_error = nullptr;
    PyObject *usage_error = nullptr;
    PyObject *unimplemented_error = nullptr;
    PyObject *missing_attribute_error = nullptr;
    PyObject *list_index_error = nullptr;
};

PyErrorTypes &py_types() {
    static PyErrorTypes types;
    return types;
}

/// Tag type backing one Python exception class.
///
/// `nb::exception<T>` is the only way to create a Python exception type, but
/// it also registers a translator for `T` -- a message-only one that would
/// drop the ErrorInfo, and that would compete with ours. Binding it to a tag
/// type nothing ever throws makes that translator permanently unreachable
/// while still giving us the class. The same trick backs `PrimopError` in
/// nix_expr.cpp.
template <int Which>
struct ErrorTag : std::runtime_error {
    using std::runtime_error::runtime_error;
};

template <int Which>
PyObject *make_error_class(nb::module_ &m, const char *name, nb::handle base = PyExc_RuntimeError) {
    return nb::exception<ErrorTag<Which>>(m, name, base).release().ptr();
}

} // namespace

NB_MODULE(errors, m) {
    m.doc() =
        "nanopynix: the Nix exception hierarchy and its single C++ -> Python "
        "translator. Importing this module is what installs the translator, so "
        "nanopynix_bindings/__init__.py imports it before anything that can "
        "raise.";

    auto &t = py_types();
    // Base classes must be created before the subclasses that name them.
    t.error = make_error_class<0>(m, "Error");
    t.eval_base_error = make_error_class<1>(m, "EvalBaseError", nb::handle(t.error));
    t.eval_error = make_error_class<2>(m, "EvalError", nb::handle(t.eval_base_error));
    t.parse_error = make_error_class<3>(m, "ParseError", nb::handle(t.error));
    t.type_error = make_error_class<4>(m, "TypeError", nb::handle(t.eval_error));
    t.undefined_var_error = make_error_class<5>(m, "UndefinedVarError", nb::handle(t.eval_error));
    t.assertion_error = make_error_class<6>(m, "AssertionError", nb::handle(t.eval_error));
    t.thrown_error = make_error_class<7>(m, "ThrownError", nb::handle(t.assertion_error));
    t.invalid_path = make_error_class<8>(m, "InvalidPath", nb::handle(t.error));
    t.unsupported = make_error_class<9>(m, "Unsupported", nb::handle(t.error));
    t.bad_store_path = make_error_class<10>(m, "BadStorePath", nb::handle(t.error));
    t.sys_error = make_error_class<11>(m, "SysError", nb::handle(t.error));
    t.usage_error = make_error_class<12>(m, "UsageError", nb::handle(t.error));
    t.unimplemented_error = make_error_class<13>(m, "UnimplementedError", nb::handle(t.error));
    // Both are nix::EvalError subclasses on the C++ side, and the bound
    // classes mirror that. What makes them worth distinguishing at all is on
    // the *public* side: nanopynix.MissingAttributeError is also a KeyError
    // and nanopynix.ListIndexError is also an IndexError, so `except KeyError`
    // works on a Nix attrset the way it does on a dict. Keeping the two
    // consistent -- both Pythonic, or neither -- is the point; a KeyError for
    // one and a NixError for the other would be the worst of both.
    t.missing_attribute_error = make_error_class<14>(m, "MissingAttributeError", nb::handle(t.eval_error));
    t.list_index_error = make_error_class<15>(m, "ListIndexError", nb::handle(t.eval_error));

    nb::detail::register_exception_translator(
        [](const std::exception_ptr &p, void * /*payload*/) {
            const auto &types = py_types();
            try {
                std::rethrow_exception(p);
            }

            // ── Not an error: cancellation ──────────────────────────
            // nix::Interrupted is a BaseError sibling of nix::Error, not a
            // subclass of it, and Python already has the right word for this.
            // KeyboardInterrupt derives from BaseException, so `except
            // Exception` correctly does NOT swallow it -- which is the whole
            // point, and is what pynix's repl and the RPC worker already
            // expect to see.
            catch (nix::Interrupted &e) {
                PyErr_SetString(PyExc_KeyboardInterrupt, e.what());
            }

            // ── nix::Error subclasses, most-derived first ───────────
            // -Wexceptions rejects this list if a base precedes its subclass.
            // nanopynix's own two, ahead of the nix::EvalError arm they derive
            // from (see nanopynix_errors.hh).
            catch (nanopynix::MissingAttributeError &e) {
                nanopynix::errinfo::raise(types.missing_attribute_error, e);
            }
            catch (nanopynix::ListIndexError &e) { nanopynix::errinfo::raise(types.list_index_error, e); }

            catch (nix::ThrownError &e) { nanopynix::errinfo::raise(types.thrown_error, e); }
            catch (nix::AssertionError &e) { nanopynix::errinfo::raise(types.assertion_error, e); }
            catch (nix::TypeError &e) { nanopynix::errinfo::raise(types.type_error, e); }
            catch (nix::UndefinedVarError &e) { nanopynix::errinfo::raise(types.undefined_var_error, e); }
            catch (nix::EvalError &e) { nanopynix::errinfo::raise(types.eval_error, e); }
            // EvalBaseError, not EvalError, is what Nix uses for
            // StackOverflowError (the max-call-depth guard), IFDError and
            // RecoverableEvalError.
            catch (nix::EvalBaseError &e) { nanopynix::errinfo::raise(types.eval_base_error, e); }
            catch (nix::ParseError &e) { nanopynix::errinfo::raise(types.parse_error, e); }
            catch (nix::SysError &e) { nanopynix::errinfo::raise(types.sys_error, e); }
            catch (nix::InvalidPath &e) { nanopynix::errinfo::raise(types.invalid_path, e); }
            catch (nix::BadStorePath &e) { nanopynix::errinfo::raise(types.bad_store_path, e); }
            catch (nix::Unsupported &e) { nanopynix::errinfo::raise(types.unsupported, e); }
            catch (nix::UsageError &e) { nanopynix::errinfo::raise(types.usage_error, e); }
            catch (nix::UnimplementedError &e) { nanopynix::errinfo::raise(types.unimplemented_error, e); }

            // ── The catch-all this whole file exists for ────────────
            // Nix throws plain nix::Error freely from libstore and libexpr
            // (nix::findPackageFilename is one measured case). Before this
            // clause every one of them arrived as a bare RuntimeError with the
            // ErrorInfo discarded.
            catch (nix::Error &e) { nanopynix::errinfo::raise(types.error, e); }

            // Anything else -- std::bad_alloc, nlohmann-json, plain
            // std::exception -- deliberately has no clause here. See the
            // header comment: it propagates and nanobind's default translator
            // takes it.
        },
        nullptr);
}
