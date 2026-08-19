"""The one Nix initialisation entry point, and the cheapest way to reach it.

**This module exists so that a program that initialises libstore does not
import the rest of nanopynix.** It imports the compiled bindings and the
feature tuple, and nothing else. Issue #123 measured the case it serves: a
planner of ``ddrn/examples/venv-graph`` spent 97% of its run on
``import nanopynix``, and the one name it read from the package was
``init_libstore``.

``from nanopynix import init_libstore`` still works, because the package
resolves each public name through a module ``__getattr__``.
"""

from __future__ import annotations

from nanopynix_bindings.util import enable_experimental_feature, init_libstore as _init_libstore_raw

from nanopynix._features import DEFAULT_EXPERIMENTAL_FEATURES


def init_libstore(load_config: bool = True) -> None:
    """Initialize libstore, then enable nanopynix's default experimental features.

    The one Nix initialisation entry point nanopynix offers. There used to be a
    second, ``init_nix``, wrapping ``nix::initNix``; it is gone because
    everything ``initNix`` adds over ``initLibStore`` is a process-wide side
    effect a library has no business imposing on its host -- a signal-handler
    thread, ``SIGCHLD`` reset to ``SIG_DFL``, a ``SIGSEGV`` handler, an
    ``NIX_SIG_MULTI_INT`` handler, ``umask(0022)``, a ``RLIMIT_NOFILE`` bump
    and a static buffer installed on ``std::cerr``. Python has its own signal
    machinery, and nothing in nanopynix ever called it.

    Enabling the features here, rather than leaving it to whoever opens a
    store, is load-bearing: Nix latches some of them at store *construction*
    but re-checks them at *query* time. ``LocalStore`` prepares its realisation
    SQL statements only when ``ca-derivations`` is on at construction
    (``local-store.cc:356``), while ``queryRealisationUncached`` re-tests the
    flag and dereferences those statements (``:1563``). A store built before
    the feature was enabled, then queried after it was turned on, therefore
    trips ``assert(stmt.stmt)`` and aborts the process -- SIGABRT, not an
    exception, so there is nothing a caller could have caught.

    Since libstore has to be initialised before any libstore call anyway, doing
    it here means every store nanopynix can open is constructed with the
    defaults already in force. ``Session`` enables the same features again
    through ``runtime.initialize``, which calls
    :func:`enable_experimental_feature` at the same point of its own sequence;
    that is additive and harmless.
    """
    _init_libstore_raw(load_config=load_config)
    _enable_default_experimental_features()


def _enable_default_experimental_features() -> None:
    for feature in DEFAULT_EXPERIMENTAL_FEATURES:
        enable_experimental_feature(feature)
