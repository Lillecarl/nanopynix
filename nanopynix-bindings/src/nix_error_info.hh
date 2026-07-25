#pragma once

/// Propagate nix::ErrorInfo across the C++ -> Python boundary.
///
/// `nb::exception<T>` can only carry `e.what()` -- a single flat string. But
/// every `nix::Error` also carries a `nix::ErrorInfo`: the severity level, the
/// source position, the evaluation trace (the `--show-trace` stack), and any
/// "did you mean ...?" suggestions. C++ is the *only* place that has this;
/// once the exception is translated to Python it is gone for good. So the
/// translator built here attaches it to the raised Python exception as two
/// attributes, which `nanopynix.exceptions.translate_nix_exception` reads back
/// into `NixError.raw` / `NixError.info`:
///
///   `raw`  -- str, the human-readable render (what Nix's CLI prints)
///   `info` -- dict, the structured form (level/msg/pos/traces/...)
///
/// Deliberately the same two names `NixError` uses. The bound classes are
/// unrelated to the public hierarchy (they are plain `RuntimeError` subclasses
/// with no `raw`/`info` of their own, so nothing collides), but "carries Nix
/// error detail" is one concept and giving it two spellings would only mean
/// every reader and every forwarding site has to know both.
///
/// Use `bind_nix_error<T>(m, "Name")` in place of `nb::exception<T>`.

#include <nanobind/nanobind.h>

#include <nix/util/error.hh>
#include <nix/util/logging.hh>
#include <nix/util/position.hh>

#include <exception>
#include <sstream>
#include <string>

namespace nb = nanobind;

namespace nanopynix::errinfo {

/// Cap on how many trace frames are reported.
///
/// Not cosmetic: this dict crosses the RPC boundary base64-encoded into the
/// `grpc-status-details-bin` HTTP/2 *header*, and a deep evaluation trace runs
/// to tens of kilobytes -- past a peer's header-list limit that manifests as a
/// protocol error rather than a degraded message. Innermost frames are the
/// useful ones, so keep the head and record that the tail was dropped via the
/// dict's `truncated` flag.
inline constexpr size_t max_traces = 32;

/// Cap on any single hint/message string, in bytes. Same rationale.
inline constexpr size_t max_hint_bytes = 4096;

inline std::string clamp(std::string s) {
    if (s.size() <= max_hint_bytes)
        return s;
    s.resize(max_hint_bytes);
    s += "... [truncated]";
    return s;
}

/// Render a source position as `{"file": ..., "line": ..., "column": ...}`.
///
/// `file` is `Pos::print`'s origin rendering rather than a raw path because a
/// position's origin is a variant -- it may be a real file, but equally a
/// string or stdin, which have no path to report.
inline nb::object pos_to_dict(const std::shared_ptr<const nix::Pos> &pos) {
    if (!pos || !*pos)
        return nb::none();
    std::ostringstream out;
    try {
        pos->print(out, /*showOrigin=*/true);
    } catch (...) {
        // Rendering an origin can consult the source; a position pointing into
        // a garbage-collected store path is expected and must not escape.
        return nb::none();
    }
    nb::dict d;
    d["file"] = nb::str(out.str().c_str());
    d["line"] = nb::int_(pos->line);
    d["column"] = nb::int_(pos->column);
    return d;
}

/// The structured form of an ErrorInfo, for `NixError.info`.
inline nb::dict to_dict(const nix::ErrorInfo &ei) {
    nb::dict d;
    d["level"] = nb::int_(int(ei.level));
    d["msg"] = nb::str(clamp(ei.msg.str()).c_str());
    d["pos"] = pos_to_dict(ei.pos);
    d["is_from_expr"] = nb::bool_(ei.isFromExpr);
    d["status"] = nb::int_(ei.status);

    nb::list traces;
    size_t emitted = 0;
    for (const auto &trace : ei.traces) {
        if (emitted++ >= max_traces)
            break;
        nb::dict t;
        t["hint"] = nb::str(clamp(trace.hint.str()).c_str());
        t["pos"] = pos_to_dict(trace.pos);
        traces.append(std::move(t));
    }
    d["traces"] = std::move(traces);
    d["truncated"] = nb::bool_(ei.traces.size() > max_traces);

    nb::list suggestions;
    for (const auto &suggestion : ei.suggestions.suggestions)
        suggestions.append(nb::str(suggestion.suggestion.c_str()));
    d["suggestions"] = std::move(suggestions);

    return d;
}

/// The human-readable render of an ErrorInfo, for `NixError.raw`.
///
/// `showTrace=false` deliberately. What the `true` branch adds is a rendered
/// source snippet *per trace frame*, and each one is produced by reading that
/// frame's source off disk (`Pos::getCodeLines` -> `Pos::getSource`). This
/// runs inside an exception translator, under the GIL, for every Nix error
/// that escapes to Python -- a path an LSP hits per keystroke -- so the cost
/// is linear in trace depth for information already present structurally in
/// `to_dict`'s `traces`. `false` still renders the single primary position,
/// which is the part worth paying for.
///
/// Either way it can throw -- a garbage-collected store path or a lazy-tree
/// origin makes the read fail -- and an exception translator is the one place
/// with nowhere to put a second error, hence the catch-all below.
inline std::string render(const nix::ErrorInfo &ei) {
    try {
        std::ostringstream out;
        nix::showErrorInfo(out, ei, /*showTrace=*/false);
        return clamp(out.str());
    } catch (...) {
        // Never let rendering an error become a second, worse error.
        return clamp(ei.msg.str());
    }
}

/// Raise *py_type* for *err*, carrying its ErrorInfo as `raw`/`info`.
inline void raise(PyObject *py_type, const nix::Error &err) {
    nb::object exc;
    try {
        exc = nb::borrow<nb::object>(py_type)(nb::str(err.what()));
        exc.attr("raw") = nb::str(render(err.info()).c_str());
        exc.attr("info") = to_dict(err.info());
    } catch (...) {
        // Degrade to what nb::exception<T> would have done rather than
        // replacing the caller's Nix error with an unrelated internal one.
        PyErr_SetString(py_type, err.what());
        return;
    }
    PyErr_SetObject(py_type, exc.ptr());
}

/// Register the Python type for a Nix C++ exception, with ErrorInfo attached.
///
/// Drop-in replacement for `nb::exception<T>`. Two translators end up
/// registered for `T`: the one `nb::exception<T>` installs (message only), and
/// ours. nanobind keeps translators in an append-to-front list traversed
/// head-first (`nanobind/src/nb_internals.h:416`), so the later registration
/// wins and `nb::exception`'s is unreachable -- it is kept only because
/// constructing `nb::exception<T>` is also what creates the Python type.
///
/// The same LIFO rule is why registration order across *types* matters, and
/// why pairing them here is load-bearing: as long as each
/// (nb::exception, ours) pair stays contiguous, registering bases before
/// subclasses still results in subclasses being tried first.
template <typename T>
nb::object bind_nix_error(nb::module_ &m, const char *name, nb::handle base = PyExc_RuntimeError) {
    nb::exception<T> py_type(m, name, base);
    nb::detail::register_exception_translator(
        [](const std::exception_ptr &p, void *payload) {
            try {
                std::rethrow_exception(p);
            } catch (T &e) {
                raise(static_cast<PyObject *>(payload), e);
            }
        },
        py_type.ptr());
    return py_type;
}

} // namespace nanopynix::errinfo
