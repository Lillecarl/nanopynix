"""The one Nix initialisation entry point, and the cheapest way to reach it.

**This module exists so that a program that initialises libstore does not
import the rest of nanopynix.** It imports the engine and the feature tuple,
and nothing else. Issue #123 measured the case it serves: a
planner of ``ddrn/examples/venv-graph`` spent 97% of its run on
``import nanopynix``, and the one name it read from the package was
``init_libstore``.

``from nanopynix import init_libstore`` still works, because the package
resolves each public name through a module ``__getattr__``.
"""

from __future__ import annotations

from nanopynix._engine import (
    enable_experimental_feature as nix_enable_experimental_feature,
    init_plugins,
    is_experimental_feature,
    load_config as nix_load_config,
    start_collector,
)
from nanopynix._features import DEFAULT_EXPERIMENTAL_FEATURES

_config_loaded = False
_plugins_loaded = False


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

    huggorm initialises libstore at import, so what is left is the
    configuration: see :func:`load_config_once`.
    """
    load_config_once(load_config)
    load_plugins_once()
    _enable_default_experimental_features()


def load_config_once(load_config: bool = True) -> None:
    """Read ``nix.conf`` and ``NIX_CONFIG``, the first time a call asks.

    huggorm reads no configuration by itself. A later call changes nothing,
    so a value a caller set after the first read stays set.
    """
    global _config_loaded  # noqa: PLW0603 -- process-wide, like the Nix configuration it reads
    if load_config and not _config_loaded:
        nix_load_config()
        _config_loaded = True


def load_plugins_once() -> None:
    """Load the plugins that ``plugin-files`` names, the first time a call asks.

    Nix loads plugins once per process, so a ``plugin-files`` value set after
    the first call has no effect. Call it after the configuration and the
    settings, and before the first store opens: a plugin registers its store
    types as it loads.
    """
    global _plugins_loaded  # noqa: PLW0603 -- process-wide, like the plugins Nix loads
    if not _plugins_loaded:
        _plugins_loaded = True
        init_plugins()


def init_libexpr() -> None:
    """Start the collector, and enable ``fetch-tree``.

    huggorm starts the collector at its first evaluator by itself. Here and
    not there, so that Nix copies ``NIX_PATH`` into ``nix-path`` as a session
    starts, and not during some later call.
    """
    start_collector()
    enable_experimental_feature("fetch-tree")


def enable_experimental_feature(name: str) -> None:
    """Add *name* to the enabled features, and mark the setting overridden.

    Raises:
        RuntimeError: Nix has no experimental feature called *name*.
    """
    if not is_experimental_feature(name):
        raise RuntimeError(f"unknown experimental feature: {name}")
    nix_enable_experimental_feature(name)


def _enable_default_experimental_features() -> None:
    for feature in DEFAULT_EXPERIMENTAL_FEATURES:
        enable_experimental_feature(feature)
