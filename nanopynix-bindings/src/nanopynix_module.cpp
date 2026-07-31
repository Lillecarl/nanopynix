/// The single extension module, and the one place binding order is decided.
///
/// See nanopynix_modules.hh for why there is one shared object rather than
/// seven.
///
/// Each area gets a submodule whose `__name__` is set to its *public* dotted
/// path before anything is bound into it. That is not cosmetic: nanobind reads
/// `__name__` off the enclosing scope when it creates a class, so this is what
/// makes a bound type report `nanopynix_bindings.expr.EvalState` rather than
/// the private `nanopynix_bindings._ext.expr.EvalState`. Repr strings, stub
/// output and `nanopynix.exceptions`'s `__module__` check all read that.
///
/// `__init__.py` publishes these into `sys.modules` under the same names, so
/// `import nanopynix_bindings.expr` keeps working exactly as it did when each
/// was its own file on disk.

#include <string>

#include <nanobind/nanobind.h>

#include "nanopynix_modules.hh"

namespace nb = nanobind;

namespace {

/// A submodule named as if it were a top-level member of the package.
///
/// `.c_str()`, not the `std::string`: casting one needs
/// <nanobind/stl/string.h>, and without it nanobind fails the conversion at
/// *runtime* with a bare `std::bad_cast` out of module init rather than at
/// compile time. `const char *` is handled by nanobind's core.
nb::module_ area(nb::module_ &parent, const char *name) {
    nb::module_ sub = parent.def_submodule(name);
    const std::string full = std::string("nanopynix_bindings.") + name;
    sub.attr("__name__") = full.c_str();
    return sub;
}

} // namespace

NB_MODULE(_ext, m) {
    m.doc() = "nanopynix: the compiled Nix bindings, as one extension module.";

    // `errors` first, and here it *is* load-bearing rather than the
    // belt-and-braces it was when Python import order decided it. Binding it
    // installs the single C++ -> Python exception translator that owns the
    // whole nix::Error hierarchy (see nix_errors.cpp), and every area bound
    // after this point can raise through it.
    auto errors = area(m, "errors");
    nanopynix_bind_errors(errors);

    // Before any area that can run Nix work: binding this only creates the
    // token type, but a caller arms a scope around calls into `store` and
    // `expr`, so the type has to exist by the time those are usable.
    auto signals = area(m, "signals");
    nanopynix_bind_signals(signals);

    auto util = area(m, "util");
    nanopynix_bind_util(util);

    auto store = area(m, "store");
    nanopynix_bind_store(store);

    auto expr = area(m, "expr");
    nanopynix_bind_expr(expr);

    auto fetchers = area(m, "fetchers");
    nanopynix_bind_fetchers(fetchers);

    // Pushes the flake-primop configurator onto
    // PyEvalState::evalSettingsConfigurators(), which is what puts
    // `builtins.getFlake` on every EvalState. That vector is read at EvalState
    // *construction*, so this only has to happen before the first one is
    // built, not before `expr` is bound -- but it is the single registration
    // now, where it used to be duplicated per shared object.
    auto flake = area(m, "flake");
    nanopynix_bind_flake(flake);
}
