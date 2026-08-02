# Where a test goes

This file maps the directories under `tests/`. Read it before you add a test
module, and choose the directory by what the test *looks at*, not by which
package the change touched.

The repository root `AGENTS.md` holds the rules that apply to all Python in
this repository, and `pytest-agent/SKILL.md` holds the procedure to run pytest
here and to read the results. This file only says where a file belongs.

## The map

| directory | holds | scope of one test |
|---|---|---|
| `tests/meta/` | self-checks: the repository examined as text and structure | the repository |
| `tests/harness/` | the test machinery itself, which no other test asserts | one harness behaviour |
| `tests/nanopynix/` | the library, by subsystem | one behaviour of nanopynix |
| `tests/pynix/` | the CLI and the LSP server | one command or one editor request |
| `tests/ekn/` | the ekn deployment tool | one ekn behaviour |
| `tests/nanopynix_helpers/` | the helpers package | one helper |
| `tests/support/` | fixtures, drivers and scanners. **No tests.** | — |
| `tests/_subprocess_startup/` | `sitecustomize.py` for a spawned interpreter | — |

`tests/nanopynix/` is the large one, and it groups by the layer under test:
`bindings/` for the compiled Nix bindings, `core/` for the direct runtime
helpers, `inproc/` and `rpc/` for the two engines, `primops/` for the Nix
primops, and the top level for what crosses those layers -- settings routing,
engine parity, the error taxonomy.

## The rule for `tests/harness/`

**A harness test asserts a thing every other test depends on, and that no
other test would notice was broken.**

`tests/support/` is full of helpers, and most of them need no test of their
own: the test that uses a helper fails when the helper breaks. A few do not
have that property, and they are what this directory is for. The first one is
`hang_report.py`, which runs only when a test has already hit its deadline. If
it returned an empty string, every run would stay green and the report would
be silently useless at the one moment it matters.

Ask whether a bug in the helper makes some other test fail. When the answer is
yes, write no test here. When the answer is "no, it just stops helping", the
helper needs its own test, and this is where the test goes.

## The rule for `tests/meta/`

**A meta test reads the repository. It does not run it.**

That is the whole line, and it decides every case so far. A meta test opens
source files, parses them, walks module objects or compares a checked-in
generated file against what the generator produces now. It needs no Nix, no
store, no subprocess and no network, and it finishes in milliseconds.

What lives there now:

| module | asserts |
|---|---|
| `test_suppression_grammar.py` | every lint or type suppression says why it exists |
| `test_agent_note_imports.py` | no test module imports `pytest_agent` directly |
| `test_consumer_surface.py` | consumers use nanopynix's public API, and its protocols rather than one engine's classes; a ledger records each exception |
| `test_public_surface.py` | `__all__` lists every public name the package binds, protocols included |
| `test_subcommands.py` | pynix's two subcommand declarations describe the same set |
| `test_docs_reference.py` | the checked-in CLI reference matches the live command tree |
| `test_docs_coverage.py` | every `__all__` name has an autodoc directive, with a ledger for the rest |
| `test_doc_snippets.py` | every published Python block mirrors a region of an example that runs |
| `test_core_has_no_getattr.py` | no `_core/` class forwards an unlisted name to a binding, untyped |

`tests/nanopynix/test_examples.py` is the case that clarifies the rule. Its
purpose is a staleness gate on the documentation, which sounds like a meta
test, but it *executes* each example against a real store. It is an
integration test, and it stays out.

## The shape of a meta test

Copy `test_suppression_grammar.py`. Three parts, and the first two are not
decoration:

1. **A "can see it" guard.** Assert that the scanner found source to scan. The
   packaged CI runner `cd`s into a store copy of the repository rather than
   running from the checkout (`nanopynix/tests.nix`), so a scanner pointed at
   a path that copy does not carry would return nothing and the conformance
   test below would pass by checking nothing. That silent no-op is the failure
   mode this whole directory exists to prevent, so every meta test guards
   against its own version of it.

2. **Scanner unit tests, in both directions.** Pin that a known-bad shape is
   caught *and* that the shapes the repository legitimately uses are not.

3. **The conformance test.** The assertion itself, with a failure message that
   names the offending file and line and says what to do about it.

Put the scanner in `tests/support/` and the test in `tests/meta/`. The existing
scanners are `suppressions.py`, `consumer_imports.py` and `docs_directives.py`;
reuse `iter_python_files` from the first rather than writing another tree walk.

**Read the structure, not the text, and read what the tool reads.**
`consumer_imports.py` walks the AST because a regular expression cannot tell an
import from the same words in a docstring. `docs_directives.py` resolves each
Sphinx directive because `:members:` documents a name that appears nowhere in
the Markdown, so a substring search reports scores of false absences. It also
mirrors what Sphinx renders rather than what is reachable: `:members:` skips an
imported member, and reading `dir()` instead made one answer depend on whether
beartype was on.

## Derived against declared

Several meta tests compare a set derived from the tree against a literal
restated in the test file -- `CONSUMER_PRIVATE_IMPORTS` here, `WIRE_CLASSES` in
`tests/nanopynix/test_exceptions_classify.py`. The friction is the point: the
literal cannot update itself, so a new member fails the suite until a person
decides whether it belongs. Write the reason in the literal, next to the entry.

Use a literal only when the answer is a judgement. `test_public_surface.py`
compares two derived sets and carries no ledger, because whether a name the
package already binds belongs in the list it publishes is not a decision.
A literal there would be a second copy of `__all__`.

Choose what the key is, and the answer differs per ledger. Key on the unit the
*judgement* covers. `CONSUMER_PRIVATE_IMPORTS` keys on `(module, name)`, because
whether a name may be reached is one decision wherever it appears, and thirty
identical failures teach nothing. `CONSUMER_ENGINE_ANNOTATIONS`, in the same
file, keys on `(file, class)` instead: whether a module may name one engine's
`Store` depends on what that module does with the store.

A ledger must also be able to get shorter. Assert both directions, so an entry
whose subject is gone fails and gets deleted, rather than sitting there as a
rubber stamp.

## What a meta test must not claim to check

Say so in the module docstring when a nearby rule is *not* machine-checkable,
so that nobody later reads the file as a wider guarantee than it gives.
Examples that came up and stay unchecked on purpose: whether prose follows
ASD-STE100, whether a name deserves to be public, and whether a recorded reason
is true. A test can prove that a reason exists. It cannot prove the reason is
honest.
