"""Every published Python snippet is a view of code that runs.

Before this file, no test executed a fenced code block, and two published
snippets did not work (#23). ``README.md`` passed
``Session(config={"max-jobs": "4"})`` to a constructor that has never had a
``config`` parameter, and the block was not even valid Python: ``async with``
at module level is a ``SyntaxError``.

The mechanism is in ``tests/support/doc_snippets.py``. A snippet carries an
``<!-- example: file#region -->`` pointer, and this file asserts that the block
equals that region. ``tests/nanopynix/test_examples.py`` runs the example, so
the two together give what #23 asks for: **the suite fails when a published
snippet stops working.** Neither half is sufficient. Execution alone lets a
page drift from the code it claims to show, and equality alone proves two
copies of something broken agree.

**A ledger records the blocks that are not executable, and why.** Some
snippets are deliberately not programs -- a two-line shape, or code that needs
a host this suite does not have. That is a judgement, so it is written down
per block rather than inferred.
"""

from __future__ import annotations

from pathlib import Path

from tests.support.doc_snippets import (
    EXAMPLES_DIR as EXAMPLES_DIR,
    Snippet as Snippet,
    documented_pages as documented_pages,
    iter_snippets as iter_snippets,
    region as region,
    region_names as region_names,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = REPO_ROOT / EXAMPLES_DIR

# Each published Python block with no pointer, and the reason it has none.
# Keyed by "<page>:<line>" -- the line moves when the page is edited, which is
# deliberate: a moved block is a changed block, and it deserves another look.
#
# Both entries need something the suite must not require. Neither is a snippet
# nobody got round to; if that changes, the entry goes and a pointer arrives.
UNMIRRORED: dict[str, str] = {
    "docs/nanopynix/api/namespace.md:23": (
        "Needs a host that allows an unprivileged user namespace, and it "
        "copies a closure into the host `daemon` store. CI runners differ on "
        "the first, and the second writes outside any store this suite owns."
    ),
    "docs/nanopynix/api/store.md:29": (
        "Two lines that need a built output to run: a `tofu` binary in a "
        "relocated store. Building one to prove two lines parse would cost "
        "more than the lines are worth."
    ),
}


def _snippets() -> list[Snippet]:
    return list(iter_snippets(documented_pages(REPO_ROOT), REPO_ROOT))


def test_the_scanner_can_see_the_pages_and_the_examples() -> None:
    """Fail loudly rather than pass by finding nothing.

    The packaged CI runner copies the repository into the store and runs from
    there, so a scanner pointed at a path that copy does not carry would
    report no snippets and every assertion below would hold vacuously.
    """
    pages = documented_pages(REPO_ROOT)
    assert any(p.name == "README.md" for p in pages), "the README is not being scanned"
    assert len(pages) > 5, f"only {len(pages)} page(s) found; the path is wrong"
    assert EXAMPLES.is_dir(), f"{EXAMPLES} is missing"
    assert _snippets(), "no Python block found in any page; the fence pattern is wrong"


def test_the_scanner_reads_a_pointer_and_a_region() -> None:
    """Both directions, on real files rather than on strings.

    The pointer must sit directly above the fence, and a region must come back
    dedented -- that is what lets a page show four lines from inside an
    ``async def`` without printing the whole program.
    """
    page = REPO_ROOT / "README.md"
    found = [s for s in iter_snippets([page], REPO_ROOT) if s.pointer is not None]
    assert found, "the README block has no pointer; this test cannot check the happy path"

    assert found[0].example is not None
    example = EXAMPLES / found[0].example
    assert example.is_file()
    body = region(example, found[0].region or "")
    assert body is not None, "the region the README points at does not exist"
    assert not body.startswith(" "), "the region came back indented; dedent is not working"
    assert region(example, "no-such-region-name") is None


def test_every_pointer_resolves() -> None:
    """A pointer at a missing file or region would silently check nothing."""
    broken: list[str] = []
    for snippet in _snippets():
        if snippet.example is None or snippet.region is None:
            continue
        example = EXAMPLES / snippet.example
        if not example.is_file():
            broken.append(f"{snippet}: no such example {EXAMPLES_DIR}/{snippet.example}")
        elif region(example, snippet.region) is None:
            broken.append(f"{snippet}: {snippet.example} has no region {snippet.region!r}")
    assert not broken, "pointer(s) naming something that does not exist:\n" + "\n".join(broken)


def test_every_published_snippet_matches_the_code_that_runs() -> None:
    """The assertion this file exists for."""
    drifted: list[str] = []
    for snippet in _snippets():
        if snippet.example is None or snippet.region is None:
            continue
        body = region(EXAMPLES / snippet.example, snippet.region)
        if body is not None and body != snippet.text:
            drifted.append(
                f"{snippet} does not match {EXAMPLES_DIR}/{snippet.pointer}\n"
                f"--- page ---\n{snippet.text}\n--- example ---\n{body}"
            )
    assert not drifted, (
        f"{len(drifted)} published snippet(s) drifted from the example that runs. "
        f"Edit the example and copy it back into the page:\n\n" + "\n\n".join(drifted)
    )


def test_every_python_block_is_mirrored_or_recorded() -> None:
    """A new snippet must point at running code, or say why it cannot."""
    unmirrored = {f"{s.path}:{s.line}" for s in _snippets() if s.pointer is None}
    declared = set(UNMIRRORED)

    added = sorted(unmirrored - declared)
    assert not added, (
        f"{len(added)} published Python block(s) point at no example, so nothing runs them.\n"
        f"Add `<!-- example: <file>#<region> -->` directly above the fence and put the code in "
        f"{EXAMPLES_DIR}/, or record why it cannot run in UNMIRRORED in this file: {added}"
    )

    stale = sorted(declared - unmirrored)
    assert not stale, f"{len(stale)} UNMIRRORED entr(y/ies) no longer match a block. Delete them: {stale}"


def test_no_example_region_is_unused() -> None:
    """A region nothing points at is dead weight, and reads as coverage.

    It also fails the other way round from the pointer test: that one catches
    a page naming a region that is gone, and this one catches a region that
    outlived the page that showed it.
    """
    used = {s.pointer for s in _snippets()}
    orphans = [
        f"{path.name}#{name}"
        for path in sorted(EXAMPLES.glob("*_example.py"))
        for name in region_names(path)
        if f"{path.name}#{name}" not in used
    ]
    assert not orphans, "example region(s) that no page shows; delete the markers or use them: " + ", ".join(orphans)
