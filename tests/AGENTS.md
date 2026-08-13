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
| `tests/gates/` | the static gates of CI, run as tools | one gate |
| `tests/nanopynix/` | the library, by subsystem | one behaviour of nanopynix |
| `tests/pynix/` | the CLI and the LSP server | one command or one editor request |
| `tests/nanopynix_helpers/` | the helpers package | one helper |
| `tests/support/` | scanners and the LSP drivers. **No tests.** | — |

The helpers are outside `tests/`, and issue #130 put them there:

| directory | holds | scope of one test |
|---|---|---|
| `test-support/src/test_support/` | helpers that name no Nix concept | — |
| `test-support/tests/` | the tests of those helpers | one helper behaviour |
| `nanopynix-testing/src/nanopynix_testing/` | fixtures and markers that name Nix | — |
| `nanopynix-testing/.../_subprocess_startup/` | `sitecustomize.py` for a spawned interpreter | — |

**A helper leaves `tests/support/` when a second project needs it.**
`tests/support/` imports as `tests.support.<name>`, which only the repository
rootdir resolves, so `grpclib-transports` and `pytest-agent` could reach none
of it, and a suite that moves into its own project could not either. Both
projects above are ordinary packages, and any project can declare one.

**One rule picks between them: does the helper name a Nix concept?** A store,
an evaluator, a linked Nix version and a worker process are Nix concepts, and
a helper that names one goes to `nanopynix-testing`. Everything else goes to
`test-support`, which must stay useful to a project that never loads Nix. Each
project's `__init__.py` carries the measurement behind its own contents.

What stays in `tests/support/` is what only this repository's own suite reads:
the scanners of the meta tests, and the LSP drivers.

**The beartype hook went with the fixtures, and it had to.** A suite that
reaches its own rootdir and cannot install the hook loses runtime type
checking and still passes every test, so the loss is silent. It names
`nanopynix`, `nanopynix_helpers` and `pynix` in its package list, which is why
it is in `nanopynix-testing` and not in `test-support`.

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
have that property, and they are what this directory is for. The first one was
`hang_report.py`, which runs only when a test has already hit its deadline. If
it returned an empty string, every run would stay green and the report would
be silently useless at the one moment it matters.

Ask whether a bug in the helper makes some other test fail. When the answer is
yes, write no test here. When the answer is "no, it just stops helping", the
helper needs its own test, and this is where the test goes.

**The same rule applies in `test-support/tests/`, and the test goes beside the
helper.** `hang_report.py` and `subprocess_output.py` moved to `test-support/`,
so their tests moved with them.

## The rule for `tests/gates/`

**A gate test runs a tool that CI already runs, and it never fails the run.**

This directory exists because of the rule below it. A meta test reads the
repository and finishes in milliseconds, and `ruff` and `pyright` are
subprocesses that take seconds, so they cannot go in `tests/meta/` without
taking that property away from every test in it.

Three things hold for each test here:

- **The gate is a copy, and not the authority.** `nix/checks.nix` builds each
  gate as a derivation, and the `static-checks` job of CI refuses the merge.
  Run the same command, so that a finding here is the finding there.
- **`xfail(strict=False)`, always.** A failing tool must not stop a developer
  from running the suite, and CI already refuses. A clean tool reports `xpass`
  and a failing one reports `xfail`, and the run stays green either way.
- **Skip when the tool is absent.** The packaged runner carries no dev shell
  tool, so `shutil.which` returns `None` there and the test skips. Do not add
  the tool to `nanopynix/tests.nix` to make it run: that job builds the
  derivation instead, and running it twice only costs time.

The order comes from `pytest_collection_modifyitems` in `tests/conftest.py`,
which puts these items after the forked tests and before everything else.

## The rule for a build with no collector

**On a build with no collector, a test that builds an evaluator runs in a fork
of the pytest process.** The `-nogc` and `-asan` variants build libexpr with
`-Dgc=disabled`, and such a build leaks by design. Nix's own package option
gives the condition that makes the leak acceptable: evaluation takes place
within short-lived processes. An RPC worker is one, and it returns every byte
when it exits. The pytest process outlives the whole suite, so it must hand
the evaluator to a child that does not.

The measurements against `nix_2_34-nogc` give the size of the difference. The
rpc share of the suite peaked at 553 MB. The in-process share demanded about
10 GB in one process, and 297 MB forked. The whole suite in one process
reached 5 GB resident with 14.6 GB of swap, and then the kernel killed it;
forked, it passed 2077 tests at a 3 GB peak.

**No flag selects this, and none should.** `build_info` publishes a `boehm_gc`
capability, and `nanopynix_testing.nix_runtime` reads it at collection time. A
build with a collector marks nothing and skips nothing.

That module holds the rule, and it finds most tests through the fixture
closure that pytest already computes. Three things follow for a new test
module:

- **A test that reaches an evaluator through `eval_state` or `inproc_session`
  needs nothing.** The rule reads `item.fixturenames`, which is transitive.
- **A test that builds one directly needs
  `pytestmark = pytest.mark.evaluator_in_process`.** Nothing in the fixture
  graph records a direct call, so `tests/meta/test_no_collector_rule.py`
  scans for one and fails until the marker is there.
- **A test that builds an `EvalState` itself must also take `init_expr`.** In
  a forked child that construction aborts unless `init_libexpr` ran in the
  parent, because `EvalState` asserts that `initGC` ran and an assert is not
  catchable. Issue #54 carries the correction.

`--in-process-evaluator` overrides the rule for a deliberate run: `=skip` is
the escape hatch when forking itself breaks, `=run` tells a fork failure apart
from a collector failure, and `=only` selects the subset for a measurement.

## The rule for a test that kills a worker

**A test that signals an rpc worker must call
`nanopynix_testing.worker_death.expect_the_worker_to_die` first.**

`Session.close` raises `WorkerSignaledError` for a worker that a signal killed
and that nothing in this process asked to stop, so that a crash inside a run
cannot report success. Issue #55 is the account: a full suite reported 2077
passed with a core dump inside its own window. A test that sends the signal
itself is the one case where that report is wrong.

The seam gates the close only. The call that was in flight still fails and
still raises, so an assertion on what the caller receives keeps its subject.

**No scanner enforces this one, and that is deliberate.** A test that forgets
fails at its own teardown, with a message that names the signal and the pid.
The other rules on this page need a scanner because they fail somewhere else,
or not at all.

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
| `test_ansi_filtering.py` | no module writes its own regular expression for an ANSI escape sequence |

`tests/nanopynix/test_examples.py` is the case that clarifies the rule. Its
purpose is a staleness gate on the documentation, which sounds like a meta
test, but it *executes* each example against a real store. It is an
integration test, and it stays out.

`tests/nanopynix/test_ci_workflows.py` is the same case, and it goes one step
further: it is a staleness gate on `.github/workflows/*.yml` that *rewrites*
the stale file before it fails. It evaluates the flake to render, so it is not
a meta test. Copy that shape for a generated file whose generator needs Nix,
and guard the write: the packaged runner is read-only, and there the
comparison alone is the result.

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

The shape is not only for `tests/meta/`. `URI_PART_STRATEGIES` in
`tests/nanopynix/test_stores_properties.py` is the same thing in an ordinary
test: it derives every field that names part of a store URI, and holds that set
against a literal that says what each field may hold. Use the shape wherever a
test needs a judgement that the code cannot supply.

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
