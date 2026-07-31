#pragma once
///@file
/// The seven binding areas, each as a function that populates a module.
///
/// These used to be seven `NB_MODULE`s in seven shared objects. They are one
/// shared object now, for one reason: a function-local static in a header is a
/// *separate object* in every hidden-visibility `.so` that includes it, and
/// nothing about that failure is loud. It cost us a real bug --
/// `PyEvalState::evalSettingsConfigurators()` (py_eval.hh) is exactly such a
/// static, so nix_flake.cpp's registration of `builtins.getFlake` never
/// reached an EvalState built by nix_expr.cpp, and nix_expr.cpp had to
/// duplicate the registration to compensate. It also forced nix_errors.cpp's
/// "everything in one translation unit" rule, which was a rule someone had to
/// remember rather than something the build enforced.
///
/// One `.so` retires the whole class of problem: a static is a static, and the
/// order these run in is decided here (see nanopynix_module.cpp) rather than
/// by whichever module Python happened to import first.
///
/// Note that one shared object is *not* one translation unit. The seven `.cpp`
/// files stay seven files and still compile in parallel; only the link is
/// merged.

#include <nanobind/nanobind.h>

void nanopynix_bind_errors(nanobind::module_ &m);
void nanopynix_bind_signals(nanobind::module_ &m);
void nanopynix_bind_util(nanobind::module_ &m);
void nanopynix_bind_store(nanobind::module_ &m);
void nanopynix_bind_expr(nanobind::module_ &m);
void nanopynix_bind_fetchers(nanobind::module_ &m);
void nanopynix_bind_flake(nanobind::module_ &m);
