#include <nanobind/nanobind.h>

#include <nix/main/shared.hh>
#include <nix/main/plugin.hh>

namespace nb = nanobind;
using namespace nb::literals;

NB_MODULE(nanopynix_main, m) {
    m.doc() = "nanopynix: Nix main bindings (initNix)";

    m.def("init_nix", &nix::initNix, nb::call_guard<nb::gil_scoped_release>(),
          "load_config"_a = true,
          "Full Nix initialization: initLibStore + signal handlers + fd limit + umask. "
          "Call this instead of init_libstore when running a daemon or long-lived process.");

    m.def("init_plugins", &nix::initPlugins, nb::call_guard<nb::gil_scoped_release>(),
          "Load external Nix plugins. Call after settings are initialized.");
}
