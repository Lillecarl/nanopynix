"""Every name in ``nanopynix.__all__`` has a page, or an entry saying why not.

``__all__`` is the public surface, and a public name with no documented home is
neither supported nor removable. Measured before this file existed: 44 of 143.

The check resolves the autodoc directives rather than searching the Markdown
for each name, and that distinction is the whole reason this file is not three
lines long. See ``tests/support/docs_directives.py`` -- a substring test reports
scores of false absences, because ``:members:`` documents a name without
writing it down.

The ledger is what made the gate landable. Without it the test could not go
green until every page existed, so it would not have been added at all, and the
drift would have gone on being invisible. It runs both ways: a new undocumented
name fails, and a ledger entry that has been documented fails so the entry gets
deleted. The list can only get shorter.

**Where a name deserves to live is a judgement, and this file does not make
it.** It reports that no page renders the name. Choosing the page, and deciding
whether the name should be public at all, stay with a person -- which is why an
entry carries a reason and an issue number rather than a ``# TODO``.

**The list is empty, and the empty list is the point.** It ran 44, then 32 when
#43 wrote ``bindings.md``, then 9, and #15 ruled on the last nine: seven got a
page and two left ``__all__``. An entry added here from now on is a name whose
status nobody has decided yet, and it needs the same treatment -- not a longer
list.
"""

from __future__ import annotations

from pathlib import Path

import nanopynix
from tests.support.docs_directives import (
    DIRECTIVE as DIRECTIVE,
    DOCS_API_DIR as DOCS_API_DIR,
    Directive as Directive,
    Documented as Documented,
    documented as documented,
    iter_directives as iter_directives,
    resolve as resolve,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
API_DIR = REPO_ROOT / DOCS_API_DIR

# Each name in `nanopynix.__all__` that no directive renders, with the reason.
#
# Empty. Every name the package publishes has a page. See the module docstring
# for what an entry here would mean, and `docs/nanopynix/api/` for where the
# last nine landed.
UNDOCUMENTED: dict[str, str] = {}


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


def test_the_scanner_reads_every_directive_that_renders_an_object() -> None:
    """A directive absent from the pattern reports its subject as undocumented.

    ``autoexception`` was the one that got away, and ``bindings.md`` uses it
    for ``PrimopError``. Both directions, so the pattern cannot pass by
    matching everything: a directive that renders nothing must stay unmatched.
    """
    for kind in ("automodule", "autoclass", "autoexception", "autofunction", "autodata", "autoattribute"):
        assert DIRECTIVE.match(f".. {kind}:: nanopynix.Thing"), f"{kind} is missing from the pattern"
    assert DIRECTIVE.match(".. toctree:: nanopynix.Thing") is None
    assert DIRECTIVE.match(".. note:: something") is None


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
