# Facts about Nix, and where each one comes from

**Every entry names the file and the line that proves it.** A fact with no
citation does not belong here. A fact that a measurement proved says which
measurement.

Line numbers follow the Nix revision that `nix/nix-master.nix` pins, and the
workspace at `~/Code/ddnix`. A line number moves. The name of the function does
not, so each entry names the function too.

## Store paths

**A derivation lives at a text content-addressed path of its own contents.**
`infoForDerivation` (`src/libstore/derivations.cc:100`) hashes the unparsed
ATerm, builds `TextInfo{hash, references}`, and calls
`makeFixedOutputPathFromCA`. `Store::writeDerivation`
(`src/libstore/derivations.cc:117`) then ends with `assert(path2 == path)`.

Three consequences follow, and all three matter:

- A text-ingested store object named `X.drv`, holding the ATerm, with the
  inputs of the derivation as its references, lands **exactly** on the
  canonical derivation path. This is why the released `builder-rpc-v0` uses
  `outputHashMode = "text"`. It is the mechanism, and not a coincidence.
- The store path of a derivation is already a content hash of that derivation.
  A reference to a derivation needs no separate hash to be self-certifying.
- The references are part of the hash, so the same ATerm with a different
  reference set gives a different path.

**A fixed output has a store path that the name gives it.**
`DerivationOutput::CAFixed::path` (`src/libstore/derivation/output.cc:26`)
calls `makeFixedOutputPathFromCA(outputPathName(drvName, outputName), ...)`.
`CAFloating` returns `std::nullopt` from `path()`
(`src/libstore/derivation/output.cc:19`), because a floating output has no
path until a build gives it one.

## Names

**`Derivation::nameFromPath` requires the `.drv` extension.**
`src/libstore/derivations.cc:263` calls `drvPath.requireDerivation()`. So a
store object that Nix reads as a derivation must be named `*.drv`, whatever
else changes.

**`builtins.derivation` guards the `.drv` name.** `src/libexpr/primops.cc:1815`
refuses a name that ends in `.drv` unless the ingestion method is `Text` and
there is exactly one output, named `out`.

**`outputHashMode = "text"` needs `Xp::DynamicDerivations`, and no `.drv`
name.** `src/libexpr/primops.cc:1570`. So an ordinary name with text ingestion
is allowed, and only the reverse pairing is not.

## Why the `.drv` naming schema exists

**The question.** The released design makes a planner carry the name
`graph.drv`, and the root that it produces carry the name `graph`. Nothing
says why, and the rule looks arbitrary. It is not: it is the end of a chain,
and each link is enforced somewhere else.

**Link 1. The `.drv` suffix is a type tag that the store enforces.**
`LocalStore::registerValidPath` (`src/libstore/local-store.cc:757`) parses
**every** store object whose name ends in `.drv`, and then calls
`checkInvariants`. So "a store object named `*.drv` is a derivation" is an
invariant of the store, and not a convention. Measured: a text object named
`junk.drv` that holds the text "this is not a derivation" cannot enter the
store at all. `nix store add` fails with `error parsing derivation '...':
expected string 'D'` (`submit-drv-broken.sh`, attribute `notADerivation`).

**Link 2. An output takes the name of the derivation.** `outputPathName`
(`src/libstore/derivations.cc:150`) gives the output `out` the name of the
derivation, and gives each other output that name with `-<output>` after it.
So a derivation whose output is a derivation has to be named `*.drv` itself.

**Link 3. Evaluation refuses that name, unless the derivation makes a real
derivation.** `src/libexpr/primops.cc:1815` permits a name ending in `.drv`
only for `ingestionMethod == Text` with exactly one output, named `out`. This
turns the failure of link 1 into an error at evaluation, with a message that
names the cause, instead of a parse error at the end of a build.

**Link 4. Text ingestion is what makes the output a real derivation, and not a
copy of one.** A text-ingested object holding the ATerm, with the inputs of
the derivation as its references, lands on the canonical derivation path. See
"Store paths" above. So the permitted shape in link 3 is exactly the shape
that produces a genuine derivation where a derivation belongs.

**Link 5. The name rule then couples the two names.**
`checkOutputs` (`src/libstore/build/derivation-check.cc:109`) requires
`outputPathName(drv.name, outputName) == info.path.name()`. This is a general
rule for every output, and not a rule about dynamic derivations: for an
input-addressed output the path is computed with that name, so it holds
without a check, and for a content-addressed output the builder chooses the
path, so the check is the only thing that enforces it.

**The result of the chain is the coupling.** The planner must be named
`graph.drv` because the root it produces is named `graph`, and the planner
computes that root when it runs. So the planner declares a result of its own
execution, in advance.

**Upstream states the intent that links 1 to 4 protect.**
`doc/manual/source/store/resolution.md:209`: "an arbitrary store object can be
read back as a derivation (as will in fact be done in case for dynamic
derivations / nested output deriving paths)."

**The relaxation of this lab breaks link 5 only.** It keeps the invariant of
link 1 by *verifying* the submitted object rather than by *naming* it: the
object must parse as a derivation, and `computeStorePath` of that derivation
must give the path where it is.

### Where each limit came from

Nobody designed the coupling. It is the intersection of three rules that
arrived twenty years apart, and no commit ever states the coupling as a goal.
Found with `jj log -r 'diff_lines(substring:"...")'` over the whole history.

**Link 3 started as a flat ban, in January 2005.** Commit `6bb5efadeceb`, by
Eelco Dolstra: "Ensure that derivation names and sources don't end in `.drv'."
It added the ban for a derivation name and for a source path in the same
change, with the message `file names are not allowed to end in '.drv'`. The
reason is link 1: at that time the suffix was already the type tag, and the ban
kept a store object that is not a derivation from wearing it.

**Link 3 became the exception it is now in October 2020.** Commit
`a4e5de1b9d26`, by John Ericson: "Derivations can output 'text-hashed' data".
It changed the flat ban into `isDerivation(drvName) && ingestionMethod != Text`,
so a derivation that makes a real derivation may carry the name. The commit
message states the ramification and its limit:

> In particular, this means that derivations can output derivations. But that
> ramification isn't (yet!) useful as we would want, since there is no way to
> have a dependent derivation that is itself a dependent derivation.

**Link 2 and link 5 came from the content-addressed derivations work, in August
2020.** Commit `e913a2989fd7`, "Squashed get CA derivations building",
introduced `outputPathName` and the comparison in `checkOutputs` in one change.
The rule exists because a fixed content-addressed output computes its path from
that name, so the name and the path have to agree.

**So the limit predates dynamic derivations, and it protects a different
thing.** Link 3 protects the type tag, link 5 protects the path of a
content-addressed output, and neither one was written with a planner in mind.
The relaxation of this lab replaces link 5 for the one case that link 3 already
permits, and it protects the type tag directly instead of through a name.

## What the store checks on its own

**`LocalStore::registerValidPath` parses every `.drv` that it registers.**
`src/libstore/local-store.cc:757` calls `readInvalidDerivation` and then
`checkInvariants`. A store object named `*.drv` that does not parse cannot
enter the store at all.

**`checkInvariants` does not verify the store path.**
`src/libstore/derivations.cc:621` compares the name in the path with
`drv.name`, and it compares the output path of each input-addressed output.
It never recomputes the whole store path. A **floating** content-addressed
derivation has no output path to compare, so nothing catches one that sits at
a path its own contents do not give. Measured: `submit-drv-broken.sh`,
attribute `wrongPath`.

## builder-rpc-v0

**The feature requires a content-addressing derivation.**
`src/libstore/unix/build/derivation-builder.cc:481`:

```cpp
if (usingSubmitted && !type(drv).isCA())
    throw Error("The builder-rpc-v0 feature may only be used with content-addressing derivations");
```

The reason is the design, and not the protocol: the submitted output **is** the
derivation, and a derivation path is a content-addressed path. Remove that
identity and the requirement loses its reason.

**A builder-rpc-v0 build gets no `$out`.** The output arrives through
`SubmitOutput`, which is worker operation 1000.

**Paths that the builder adds are referenceable.**
`src/libstore/unix/build/derivation-builder.cc:1105` builds
`referenceablePaths` from the input paths, the scratch outputs, **and**
`state_.lock()->addedPaths`. `scanForReferences` runs over the output at line
1211. So an ordinary output that names a path the builder registered gets that
path recorded as a reference, with no declaration and no `--scan`.

**The allowlist of the restricted socket.** `src/libstore/daemon.cc`, in
`performOp` under `RecursiveFlag::RecursiveSubmitted`: `AddToStore`,
`AddMultipleToStore`, `AddToStoreNar`, `AddToStoreScanning`, `SubmitOutput`,
`AddTempRoot`, `IsValidPath`, and `EnsurePath` after the patch of this lab.
The refusal reads `Operation %d not allowed inside derivation`. `EnsurePath`
is operation 10.

**`builtins.storePath` and `builtins.appendContext` each make one daemon
call, and it is `ensurePath`.** `src/libexpr/primops.cc` and
`src/libexpr/primops/context.cc:274`. Every other store call of those two
primops is path arithmetic that `StoreDirConfig` answers with no daemon.

**The mount table does not save `builtins.storePath`.** That primop skips
`ensurePath` when `state.storeFS->getMount(...)` finds the path, and the
closure is bind-mounted in the sandbox, so the guard looked like it might fire
already. Measured with the allowlist entry removed and Nix rebuilt: both
primops fail with `Operation 10 not allowed inside derivation`. The mount
table fills lazily and holds no entry for a closure path that nothing touched.

**`RestrictedBuilder::ensurePath` substitutes nothing.**
`src/libstore/restricted-store.cc:275` asserts `goal.isAllowed(path)` and then
does nothing. `isAllowed` means the path is in `inputPaths`, or this builder
added it. So the operation reaches no further than `IsValidPath` does.

## Submission, and what the daemon checks

**`SubmitOutput` accepts an `Opaque` deriving path only.**
`src/libstore/unix/build/derivation-builder-impl.hh:192`. A `Built` path gives
`Attempted to submit Built path '%s' for output '%s'.\n Only Opaque paths are
supported`, and the message links NixOS/nix#12727. A caller that holds a
derivation must therefore reduce it to the store path of that derivation.

**`checkSubmittedOutputs` is the whole of the submission check.**
`src/libstore/unix/build/derivation-builder.cc:1700`. It reads the path info of
each submitted path, runs `checkOutputs` with `OutputSource::Submitted`,
refuses a declared output that no submission covered, asserts that the output
is `CAFixed` or `CAFloating`, and registers one realisation for each output.
Nothing else runs. The output gets no signature, because only the realisation
matters.

**The ingestion method must agree, and this is why `outputHashMode = "text"`
stays necessary for a planner that submits a derivation.**
`src/libstore/build/derivation-check.cc:53`, in the `CAFloating` branch of
`checkCAOutput`:

```cpp
if (info.ca->method != dof.method)
    throw BuildError(... "was hashed with method '%s', expected '%s'" ...);
```

Every derivation ingests as `text`, because `writeDerivation` adds the ATerm
with text ingestion. So a planner that submits a derivation declares `text`,
and a planner that submits an ordinary directory declares `nar`.

**The `Deferred`, `Impure` and `InputAddressed` branches of `checkCAOutput` do
nothing.** Same file, line 74. The check is meaningful only for a
content-addressing output, which is the output kind that `builder-rpc-v0`
requires anyway.

## The evaluator inside a build

**`EvalState::coerceToSingleDerivedPathUnchecked` needs a string.**
`src/libexpr/eval.cc:2660` calls `forceString` and then requires exactly one
context entry. An attribute set that is a derivation fails with `expected a
string but found a set`, so a caller reduces the derivation through its
`outPath` attribute first. `state->s.outPath` is the symbol, and
`state->isDerivation(v)` is the test.

**A derivation stands for its output, so `--submit` takes the `drvPath` field
of the resulting `Built` path.** The build that runs now makes the derivation.
No build made the output of that derivation yet, so the output is not a store
object that anything can submit.

**The evaluator inside the sandbox gives the same store path as an evaluator
outside it.** `tests/functional/dyn-drv/eval-submit.sh` builds the same graph
through two routes: one planner evaluates it directly, and a second planner two
levels down evaluates it inside a second sandbox. The test asserts that the two
root derivation paths are equal, and they are.

**`builtins.toFile` records the store paths of the context of its argument as
references of the file that it makes.** So an expression that interpolates
another `toFile` path reaches the sandbox with the whole closure, as an input
of the derivation that reads it. This is how a nested planner gets its script.

**`builtins.storePath` is what gives a plain path a context inside the
sandbox.** A path that an expression holds as literal text is no input of the
derivation that the evaluator writes. `builtins.storePath` asks the daemon,
which is `EnsurePath`, and the restricted socket answers only because of the
allowlist entry of this lab. The test asserts the resulting `inputs.srcs`
entry.

**`builtins.outputOf` chains as deep as the graph goes.** The same test follows
three levels: a planner submits the derivation of a second planner, that
planner submits the root of a graph, and the root gives the file.

**Every kind of child works under a submitted root.** The test covers a graph
of floating content-addressed derivations, a graph of input-addressed
derivations, and an input-addressed root over one floating child. The third
case makes the root deferred, so the root has no output path until the child is
built.

**nanopynix reaches the same result, and `ddrn/examples/evaluated-graph` is the
measurement.** A derivation named `planner` submitted
`/nix/store/ydwl54aq…-graph.drv`, and the graph realised to a directory holding
`alpha` and `beta`. The route is `EvalState.eval_string`, then
`Value.derived_path`, then `Store.submit_output`. No ATerm, and no `nix`
binary.

**The two kinds of child look different in the ATerm, and the difference is
visible in that run.** `nix derivation show` of the submitted root gives:

```json
"args": ["-c", "mkdir -p \"$out\"\ncp /1jgic4bscb63… \"$out/a\"\ncp /nix/store/5q9bz587…-leaf-b \"$out/b\"\n"]
```

`leaf-a` floats, so the evaluator wrote a downstream placeholder, which the
build rewrites. `leaf-b` is input-addressed, so the evaluator wrote the real
output path. One `EvalState` computed that path with no read of a `.drv` back
out of the store, which is the read that the allowlist refuses.

**`inputs.srcs` of that root holds bash and coreutils.** Each one reached the
derivation through `builtins.storePath`, and so through `EnsurePath`. Without
the allowlist entry of this lab, the expression stops at that primop.

**The realised root sits at a different path from the submitted root.** The run
submitted `ydwl54aq…-graph.drv` and built `cz50wrj9…-graph.drv`. The second is
the *resolved* derivation: a floating child has a real output path once it is
built, and resolution rewrites the placeholder to it. This is ordinary for a
content-addressed graph, and it is not a symptom.

## A pre-instantiated menu, and how a planner uses one

**`builtins.appendContext` turns a bare output path back into a dependency.**
`src/libexpr/primops/context.cc:304`, the `outputs` branch, makes a
`NixStringContextElem::Built` for each named output of the key:

```cpp
context.emplace(NixStringContextElem::Built{
    .drvPath = makeConstantStorePathRef(namePath),
    .output = std::string{outputName},
});
```

So `builtins.appendContext out { ${drv} = { outputs = [ "out" ]; }; }` gives a
string that *is* the output path and that *depends on* the derivation. A
derivation that interpolates it records `drv` as an input.

**That primop calls `ensurePath` on the key** (same file, line 275), so it
reaches the allowlist entry of this lab. A planner cannot use a
pre-instantiated menu without it.

**The two `unsafeDiscard*` builtins are what keep the menu lazy, and they are
the inverse of the line above.** `unsafeDiscardOutputDependency` leaves the
`.drv` as an input source while the output stays unbuilt.
`unsafeDiscardStringContext` gives the output path with no dependency at all.

**`builtins.toJSON` keeps the string context of what it serialises.** So a
menu of store paths in one environment variable makes every path in it an
input source of the derivation. `ddrn/examples/venv-graph/default.nix` passes
21 artefacts, 6 tools and 3 scripts that way, in `DDRN_MENU`.

## Python packaging, measured

**A graph whose nodes the planner chose, and whose shape it decided, builds and
runs.** `ddrn/examples/venv-graph` resolved 21 artefacts down to 4 nodes, 3 of
them members of the environment:

```text
plan: certifi: certifi-2024.8.30-py3-none-any.whl (wheel)
plan: charset-normalizer: charset_normalizer-3.4.4-cp314-cp314-manylinux2014_x86_64…whl (wheel)
plan: colorama-0.4.6-py2.py3-none-any.whl: marker excludes this target
plan:   backend flit_core-4.0.2-py3-none-any.whl
plan: idna: idna-3.18.tar.gz (sdist)
```

**One generated derivation feeds another, and the second one could not exist
before the plan ran.** `nix derivation show` of the `idna` node:

```text
node: idna-3.18
  inputDrvs:
    fr6sp6rp…-idna-3.18.tar.gz.drv
    hlr53jdq…-flit-core-4.0.2.drv
  backendPath: /1pf0x0b8…/lib/python3.14/site-packages
  backend: flit_core.buildapi
```

`backendPath` is a downstream placeholder, because the backend node floats.
The planner read `flit_core` from the `pyproject.toml` requirement recorded in
the lock file, resolved that name against the same lock file, and made the
wheel of it a node.

**A PEP 517 backend builds offline with nothing but itself on `PYTHONPATH`.**
Measured outside Nix first, and then inside the sandbox:
`flit_core.buildapi.build_wheel` over the `idna` sdist gives
`idna-3.18-py3-none-any.whl`, with no network and no index.

**The environment runs.** `idna.encode("ドメイン.テスト")` gives
`xn--eckwd4c7c.xn--zckzah`, from a package that the graph built from source.

**A change to the planner rebuilds the planner and nothing else.** A later edit
to `plan.py` changed the store path of the planner, so the planner ran again.
It submitted the same root, `x6bjdmvp…-demo-venv.drv`, and the environment
stayed at `9jsa78yq…-demo-venv`. The graph is a function of the decisions, and
not of the source that made them, so content addressing stops the rebuild at
the planner. This is the property that the single-derivation form cannot have:
there, one install step holds every wheel, and any change rebuilds all of
them.

### Installing a wheel, and what `unzip` does not do

**`unzip` is not an install.** `pyproject.nix` uses `pypa/installer`
(`build/hooks/pypa-install-hook`), and the difference is visible from inside
the environment: `importlib.metadata.distributions()` finds a package only when
an installer wrote its `.dist-info`, with `RECORD` and `METADATA`. A console
script exists only when an installer read `entry_points.txt` and wrote it.

**The installer is a node of the graph, and it bootstraps by unpacking.**
Nothing can install the installer, so the planner gives that one artefact a
node that unzips it. Every other node then installs with it. This is the
bootstrap that `pip` performs, expressed as one node.

**A store path is not a wheel file name.** `installer` reads the name of the
file to learn which `.dist-info` the wheel must carry, and a store path puts a
hash in front of that name:

```text
installer.sources._WheelFileBadDistInfo: Wheel .dist-info directory doesn't
match wheel filename (filename='/nix/store/m5r5inpx…-certifi-2024.8.30-py3-none-any.whl',
dist_info='certifi-2024.8.30.dist-info')
```

So the wheel goes under its own name first. nixpkgs and `pyproject.nix` install
from a `dist` directory for the same reason.

**A console script needs its shebang rewritten.** The shebang that `installer`
writes names the interpreter that ran the install, which is the one in the
store, and that interpreter does not see the environment. `pyproject.nix` calls
the fix `write_bin`. Match the shebang on the *name* of the interpreter, and
not on its whole path: `installer` takes the shebang from `sys.executable`, and
CPython reports that after it resolves the symlink, so an install run through
`python3` gives a shebang that names `python3.14`.

**`venv.EnvBuilder` gives a real environment.** The result has `pyvenv.cfg`,
`bin/python` and `bin/activate`, and `sys.prefix` is the environment, so an
import needs no `PYTHONPATH`. Measured from inside:

```text
interpreter  /nix/store/w9pzpvlp…-demo-venv/bin/python
prefix       /nix/store/w9pzpvlp…-demo-venv
entry points ['idna=idna.cli:main']
```

**Bytecode makes an installed package self-referential.** `installer` compiles
`.pyc` by default, and a `.pyc` embeds the absolute path of its source, which
is inside the output. Measured on `certifi`: 6 files hold the hash of their own
store path, and `path-info` records the path in its own `references`. A
floating content-addressed output takes this in its stride, because Nix
computes the hash modulo the self-reference.

**Unresolved, and it belongs to the harness and not to the graph.**
`nix copy --from <chroot store>` of such a path into the daemon of this
machine, which runs Nix 2.34, fails:

```text
error: ca hash mismatch importing path '/nix/store/2h0bgg8v…-certifi-2024.8.30';
         specified: sha256:1bq7qm90rqvml05gzii2ic6w9kyq8knjmnmasd188v1iwasl68yz
         got:       sha256:0dnahkz7iaic6q5bpr7dixwsj76yr83g056d8hjcjphydjv3c4hz
```

The path has a self-reference, so the two sides have to agree on the hash
modulo that reference. **This is not root-caused.** The graph itself is not
affected: `ddrn/examples/venv-graph/check.nix` runs the environment inside the
store that holds it, and it passes.

**A check of a store output runs as a derivation.** Every symlink that
`make-venv.py` writes is absolute, as every store path is, and a chroot store
puts the store under a prefix. Those symlinks resolve from inside a build of
that store, and not from a shell outside it. `check.nix` interpolates
`builtins.outputOf planner.outPath "out"` into an ordinary derivation, which is
also the consumer side of the whole feature.

### A binary wheel, and the loader that it names

**A manylinux wheel names a loader that this system does not have.** Measured
on `ninja-1.13.0-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl`,
which ships an ELF executable at `ninja-1.13.0.data/scripts/ninja`:

```text
interpreter: /lib64/ld-linux-x86-64.so.2
needed:      libstdc++.so.6 libm.so.6 libgcc_s.so.1 libpthread.so.0 libc.so.6
rpath:       ''
```

Running that file gives `Could not start dynamically linked executable`, and
neither `libstdc++.so.6` nor `libgcc_s.so.1` is on the default search path of
this system.

**`auto-patchelf` of nixpkgs corrects both, and a graph node calls it
directly.** `pyproject.nix` puts `autoPatchelfHook` in `nativeBuildInputs`
(`build/hacks/default.nix`), but a node of this graph is a plain `derivation`
and runs no setup hook. `pkgs.auto-patchelf` is the program that the hook
calls. It needs `patchelf` on the path, and it needs `NIX_BINTOOLS` to name a
bintools wrapper: it reads the loader from `nix-support/dynamic-linker` and the
libc from `nix-support/orig-libc`. The same file, after the node installed it:

```text
interpreter: /nix/store/l8si8gnv…-glibc-2.42-67/lib/ld-linux-x86-64.so.2
rpath:       /nix/store/0iv8glcs…-gcc-15.3.0-lib/lib:/nix/store/kw26hfd5…-gcc-15.3.0-libgcc/lib
```

**A shared library of a wheel often needs no patch, and an executable always
does.** The two extension modules of `charset_normalizer` need `libc.so.6` and
`libpthread.so.0` and nothing else, and the interpreter that loads them has
already loaded both, so they work with no RPATH at all. The failure appears
when the wheel carries an executable, or a library that needs more than libc.

**The tag of a wheel says which node needs this.** A wheel tagged `any` carries
no compiled code, so the planner marks only the other ones. Measured: the node
of `certifi` references itself and nothing else, and the node of `ninja`
references `gcc-15.3.0-lib`, `gcc-15.3.0-libgcc` and `glibc`. Every one of
those three is already in the closure of `python3`, so the environment grew by
0.4 MiB, which is the size of `ninja`.

### An editable install, and how it survives the store

**A PEP 660 wheel carries a path, and not code.** `flit_core` writes one file,
`myapp.pth`, whose whole content is the directory that the backend ran in.
Under Nix that directory is a copy of the project inside the store, which is
read only. So the install has to be redirected after it is made.

**`os.path.expandvars` is what makes the redirection outlive the build.**
`pyproject.nix` wraps every replaced path in it (`build/hooks/editable_hook`,
`patch_editable.py`), and the reason is a store path: a literal root is part of
what the node builds, so a different root gives a different environment. A
reference to a variable is the same bytes for every tree. Measured, from the
node output:

```text
import sys; import os.path; sys.path.append(os.path.expandvars('$DDRN_EDITABLE_ROOT/src'))
```

`ddrn/examples/venv-graph/run.sh` builds two check derivations that name two
different trees, and both read `rwl64yg3…-demo-venv`. One environment, two
sources, no rebuild.

**A `.pth` line that is a bare path cannot expand a variable.** `site` runs a
line that starts with `import`, and appends any other line to `sys.path`
verbatim. So the rewrite has to turn the path into code, which is what
`patch_pth` of `pyproject.nix` does and what this lab copies.

**A backend that writes an import finder needs more than a text rewrite.**
setuptools puts the build path inside a string literal of a generated `.py`
file. `pyproject.nix` rewrites that literal as a concrete syntax tree, with
`libcst`, so that it changes the literal and nothing else. This lab uses
`flit_core`, which writes the `.pth` form only, and raises rather than pretend
to handle the other form.

**The editable node holds no source.** Measured, the whole output:

```text
bin/myapp
lib/python3.14/site-packages/myapp.pth
lib/python3.14/site-packages/myapp-0.1.0.dist-info/{METADATA,RECORD,WHEEL,entry_points.txt}
```

**A local project declares its backend the way a source distribution does, and
the planner resolves both against the same lock file.** The planner reads
`build-system.requires` from the `pyproject.toml` of the project, parses each
entry as a PEP 508 requirement, normalises the name by PEP 503, and matches the
version by PEP 440. Measured: `myapp` asks for `flit_core >=3.11,<5`, the lock
file calls that package `flit-core`, and the node that the `idna` source
distribution already needed is the node that the editable gets.

## Deriving paths

**A `SingleDerivedPath` names a derivation only when it is the `drvPath` field
of another deriving path.** Every other position resolves an output path,
which may be any store object.

**These call sites resolve a `drvPath` and then read the result as a
derivation.** Measured by conversion, and by the two failures that the missed
ones produced:

- `src/libstore/misc.cc`, both `resolveDerivedPath` overloads
- `src/libstore/build/derivation-trampoline-goal.cc`, lines 83 and 114
- `src/libstore/legacy-ssh-store.cc:359`, `src/libstore/remote-store.cc:688`,
  `src/libstore/restricted-store.cc:345`
- **`src/nix/build.cc:38` and `:57`**, the `toJSON` fallback, which then calls
  `queryPartialDerivationOutputMap` on the result
- **`src/libcmd/installables.cc:725`**, `toDerivations`
- **`src/nix/log.cc:40`**, and **`src/libexpr/primops/context.cc:201`**
  (`builtins.getContext`)

`src/libstore/derivations.cc:400` is **not** one of them. `tryResolve` resolves
an output path there, and the `drvPath` inside that call is already a separate
resolution.

**An indirection that only the goal follows breaks the last four.** Measured on
the `.drvref` branch, against a live store:

```
nix build --dry-run --json  → error: error parsing derivation
                                '/nix/store/…-graph.drvref': store path … is
                                not a valid derivation path
nix derivation show         → {"derivations":{},"version":4}
```

## Resolution, and when a path is known

**A static output path needs no build, and gets no validity check.**
`Store::queryStaticPartialDerivationOutput` (`src/libstore/store-api.cc:458`)
reads the derivation and returns `outputsAndOptPaths(drv)`. An
**input-addressed** output therefore resolves to its path before anything
builds it, and nothing tells the caller that the path holds no store object
yet.

**A floating output resolves to nothing until a realisation exists.**
`deepQueryPartialDerivationOutputImpl` (`src/libstore/outputs-query.cc`)
returns the static result at once when there is one, and only a floating
output falls through to `queryRealisation`. Before the build there is no
realisation, so the result is `std::nullopt`, and `resolveDerivedPath` then
throws `MissingRealisation`.

**The trampoline tolerates exactly one exception.**
`src/libstore/build/derivation-trampoline-goal.cc:83`:

```cpp
try { drvPath = resolveDerivedPath(worker.store, *drvReq); }
catch (MissingRealisation &) { return std::nullopt; }
```

`std::nullopt` means "go and obtain the derivation first", which is the branch
that builds the planner. Any other exception escapes and fails the goal.

**Consequence for a `.drvref` follow inside `resolveDerivedPath`.** The follow
reads a store object, so it must not raise a plain error when that object does
not exist yet:

- With a **content-addressed** planner the follow is never reached too early.
  `MissingRealisation` fires first, the planner builds, and the second
  resolution follows a reference that exists.
- With an **input-addressed** planner the path resolves statically, so the
  follow is the first thing that touches the store, and a plain `InvalidPath`
  escapes the trampoline before it can build the planner. The follow has to
  signal "not yet" the way a missing realisation does.

## Substitution

**A substitution realises every reference first.**
`PathSubstitutionGoal` (`src/libstore/build/substitution-goal.cc:129`) makes a
substitution goal for each reference before it fetches the path, and it then
checks that each reference is valid (line 184). So a `.drvref` store object
that records the derivation as a reference brings that derivation with it
whenever it is substituted. The closure invariant does this, and no new code
is needed.

**`queryMissing` skips a dynamic derivation.** `Store::queryMissing`
(`src/libstore/misc.cc:197`) handles a `DerivedPath::Built` only when its
`drvPath` is `Opaque`, and otherwise warns "Ignoring dynamic derivation %s
while querying missing paths; not yet implemented". So `nix build --dry-run`
cannot say what a dynamic derivation will build. This is upstream behaviour
today, and it affects every design in this lab equally.

## Traps

**`nix derivation show` wraps its result.** The shape is
`{"derivations": {"<base name>": {...}}, "version": 4}`. A `jq` filter of
`.[].inputs.drvs` finds nothing; the filter is `.derivations[].inputs.drvs`.

**`inputs.drvs` is keyed by the base name of the store path**, and not by the
whole path. Schema `derivation-v4.yaml`.

**A store path sorts by its hash.** `sort` over a list of store paths gives an
order that has nothing to do with the names. Strip the hash first, or assert
without an order. This cost two test failures in one session.

**`nix develop --command` does not define the stdenv phase functions.** It
exports `$stdenv` and `$mesonFlags`, so `configurePhase` is not a command.
Drive meson and ninja directly. Entry costs about 12 seconds, for the flake
and for `--file shell.nix` alike, so batch the work into one invocation.

**`meson test` rebuilds before it runs.** A command of the form
`ninja -C build && meson test -C build` therefore prints `ninja: no work to
do` from the second build, after the first one did the work. Do not read that
line as proof that a source edit went uncompiled. Compare the mtime of the
object file with the mtime of the source.

## Upstream defects that this lab found

**`SubmitStore::require` has no definition.** An undefined symbol.

**`Store::writeDerivation` does not prime `nix::derivation::hashes`.** The memo
that `pathInputModulo` reads (`src/libstore/derivation/aterm.cc:745`) is
process-global, and a miss calls `store.readInvalidDerivation`, which the
restricted allowlist refuses. A planner avoids the memo by giving the graph
floating content-addressed outputs, so that no output path comes from a hash
modulo the inputs.

Neither is reported upstream.
