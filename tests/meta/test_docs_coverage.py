"""Every name in ``nanopynix.__all__`` has a page, or an entry saying why not.

``__all__`` is the public surface, and a public name with no documented home is
neither supported nor removable. Measured before this file existed: 44 of 143.

The check resolves the autodoc directives rather than searching the Markdown
for each name, and that distinction is the whole reason this file is not three
lines long. See ``tests/support/docs_directives.py`` -- a substring test reports
scores of false absences, because ``:members:`` documents a name without
writing it down.

The ledger is what makes the gate landable today. Without it the test could not
go green until every page existed, so it would not have been added at all, and
the drift would have gone on being invisible. It runs both ways: a new
undocumented name fails, and a ledger entry that has been documented fails so
the entry gets deleted. The list can only get shorter.

**Where a name deserves to live is a judgement, and this file does not make
it.** It reports that no page renders the name. Choosing the page, and deciding
whether the name should be public at all, stay with a person -- which is why
the 32 entries below carry a reason and an issue number rather than a
``# TODO``.
"""

from __future__ import annotations

from pathlib import Path

import nanopynix
from tests.support.docs_directives import (
    DOCS_API_DIR as DOCS_API_DIR,
    Directive as Directive,
    Documented as Documented,
    documented as documented,
    iter_directives as iter_directives,
    resolve as resolve,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
API_DIR = REPO_ROOT / DOCS_API_DIR

_BINDINGS = (
    "Defined in the compiled `nanopynix_bindings` extension, which has no "
    "reference page at all. #43 owns that page, because these need written "
    "text and not a directive: the docstrings come from nanobind signatures."
)
_IS_IT_PUBLIC = (
    "A public name whose home raises the question of whether it should be "
    "public. #15 owns that question, and a page cannot be written before it "
    "is answered -- documenting the name would settle it by accident."
)

# Each name in `nanopynix.__all__` that no directive renders, with the reason.
#
# Two groups, and no third. Everything that only needed a directive has one
# now: the namespace API, `StoreImpl`, `LogCollector`, `normalize_log_level`,
# `LogLevelInput`, `DerivedPath`, `DEFAULT_EXPERIMENTAL_FEATURES`, the six
# yaml primops, and the seven proto-derived data models that `models.md`
# already existed to hold.
UNDOCUMENTED: dict[str, str] = {
    # -- The compiled bindings (23). #43. ------------------------------
    "BuildMode": _BINDINGS,
    "EvalState": _BINDINGS,
    "PrimopError": _BINDINGS,
    "Value": _BINDINGS,
    "build_info": _BINDINGS,
    "current_system": _BINDINGS,
    "enable_experimental_feature": _BINDINGS,
    "eval_file": _BINDINGS,
    "get_flake": _BINDINGS,
    "get_verbosity": _BINDINGS,
    "init_libexpr": _BINDINGS,
    "input_from_attrs": _BINDINGS,
    "input_from_url": _BINDINGS,
    "install_logger": _BINDINGS,
    "list_settings": _BINDINGS,
    "lock_flake": _BINDINGS,
    "open_store": _BINDINGS,
    "parse_flake_ref": _BINDINGS,
    "process_connection": _BINDINGS,
    "register_primop": _BINDINGS,
    "register_store_implementation": _BINDINGS,
    "remove_logger": _BINDINGS,
    "set_verbosity": _BINDINGS,
    # -- Is it public? (9). #15. ------------------------------------------
    "DISPATCHABLE_METHODS": ("A tuple with no `__module__`, so there is no page it belongs to. " + _IS_IT_PUBLIC),
    "GcRoot": (
        "A generated proto message. The seven that a caller receives joined "
        "`models.md`. This one is a store internal a caller does not "
        "construct. " + _IS_IT_PUBLIC
    ),
    "LogLevel": (
        "A generated proto enum. `normalize_log_level` and `LogLevelInput` "
        "are the documented way in, so a page here asserts the raw enum is "
        "also a name to reach for. " + _IS_IT_PUBLIC
    ),
    "ResultType": ("The enum inside `BuildResult`, and reachable through that documented class. " + _IS_IT_PUBLIC),
    "ValueHandle": (
        "An rpc wire type -- an integer handle for a worker-side value, and "
        "an implementation detail of one engine's transport. " + _IS_IT_PUBLIC
    ),
    "init_libstore": (
        "Defined on the `nanopynix` package itself rather than in a module, "
        "so no `automodule` reaches it. " + _IS_IT_PUBLIC
    ),
    "rpc": (
        "The submodule, exported as a name. Its contents are documented "
        "across `session.md`, `store.md` and `eval.md`; the module object "
        "itself is not a thing autodoc renders. " + _IS_IT_PUBLIC
    ),
    "set_manager_title": (
        "Lives in `nanopynix._process_title`, a private module. A public name with a private home. " + _IS_IT_PUBLIC
    ),
    "strip_ansi": (
        "Re-exported from the third-party package of the same name. "
        "Documenting it would promise a function this project does not own. " + _IS_IT_PUBLIC
    ),
}


def test_the_directive_scanner_finds_the_docs() -> None:
    """Fail loudly if the gate is pointed at an empty directory.

    The conformance test below compares a derived set against a literal. A
    scanner that found no directives would report every name as undocumented,
    which fails loudly -- but a scanner that found no *names* would report
    none, and pass while checking nothing.
    """
    assert API_DIR.is_dir(), f"{API_DIR} is missing; is the source tree present?"
    directives = list(iter_directives(API_DIR))
    assert len(directives) > 20, f"only {len(directives)} directives found; the pattern is wrong"
    rendered = documented(API_DIR)
    assert id(nanopynix.NixError) in rendered.ids, "a known-documented class is missing; the resolver is wrong"


def test_every_directive_resolves() -> None:
    """A directive naming something that does not exist renders nothing.

    Sphinx warns and moves on, so the page loses a class in silence. This is
    also the guard on the resolver: a directive that stops resolving because
    the code moved fails here rather than quietly widening the report below.
    """
    broken = [d for d in iter_directives(API_DIR) if resolve(d.target) is None]
    assert not broken, "autodoc directive(s) name something that does not exist:\n" + "\n".join(str(d) for d in broken)


def test_the_resolver_reads_directives_and_not_text() -> None:
    """The distinction this whole file rests on, pinned.

    ``nanopynix.NixError`` is documented by ``.. automodule::
    nanopynix.exceptions`` with ``:members:``, and its name appears in no
    directive line. A substring search over the Markdown would report it
    missing. Resolving does not.
    """
    rendered = documented(API_DIR)
    assert "nanopynix.NixError" not in rendered.names
    assert "nanopynix.exceptions.NixError" in rendered.names


def test_every_public_name_has_a_page_or_a_reason() -> None:
    rendered = documented(API_DIR)
    missing = {
        name
        for name in nanopynix.__all__
        if id(getattr(nanopynix, name, None)) not in rendered.ids and f"nanopynix.{name}" not in rendered.names
    }
    declared = set(UNDOCUMENTED)

    added = sorted(missing - declared)
    assert not added, (
        f"{len(added)} name(s) in nanopynix.__all__ have no documented home.\nAdd a directive "
        f"under {DOCS_API_DIR}/, or record why the page cannot be written yet in UNDOCUMENTED "
        f"in this file: {added}"
    )

    stale = sorted(declared - missing)
    assert not stale, (
        f"{len(stale)} entr(y/ies) in UNDOCUMENTED now have a page. Delete them; the list is "
        f"meant to get shorter: {stale}"
    )
