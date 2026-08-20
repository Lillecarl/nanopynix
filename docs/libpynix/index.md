# libpynix

`libpynix` is the command-line layer of `pynix`, as a library. A program
declares each command as a class, and this library builds the `argparse`
parser, answers a shell completion and dispatches.

It knows nothing about Nix, and it depends on no part of `nanopynix`. A
program that takes it therefore takes the parser without the evaluator.
`libpynix.nix_options` is the one module that names a Nix concept, and it
declares `--file`, `--flake` and `--attr` without reading any of them.

Issue #222 made this a library. Before it, `pynix/src/pynix/_cli.py` was 359
lines and `easykubenix` carried a copy of the same lines; the two diverged in
six days.

## Declare a command, and mount it

```{literalinclude} ../../libpynix/examples/minimal_example.py
:language: python
```

Three things make that file work:

- **An annotated class attribute is an option.** The annotation decides what
  the parser does with the value. `bool` becomes a flag, `list[str]` becomes a
  repeated option, and `int`, `float` and `Path` are converted rather than
  handed back as the string the caller typed. A
  `Literal["yaml11", "yaml12"]` becomes the set of words the parser checks and
  the shell offers, so a declaration never has to write them a second time in
  its help text.
- **The docstring is the help.** The first line is what the subcommand list
  prints, and the whole docstring is what `--help` prints for that command.

  **Write a one-line summary on that first line**, as PEP 257 asks. The
  subcommand list has one short column for it, and this layer takes the line
  and not the first paragraph on purpose: of the 22 commands of `pynix`, one
  has a first paragraph of two sentences, and taking the paragraph would put
  "Use --rip to actually delete them." into that column beside
  `print-dead`.
- **`subcommands` mounts a tree.** A class with subcommands and no `run` is a
  group. `libpynix.group` declares one in a single expression, for a group
  that needs no class of its own.

`pos()` declares a positional instead of an option, and it converts the same
way: `where: Path = pos(help="...")` arrives as a `Path`. A positional with no
`default` is one the caller must give, one with a `default` becomes optional,
and a `list[str]` positional takes whatever is left.

`opt(..., required=True)` is the option a caller must name. It is for an
option with no sensible default, such as the destination of a push. Do not
combine it with `configured=True`: a configured option has a source below the
command line, so requiring one at the parser would refuse a value that the
configuration file already gives, and `opt` raises rather than build that.

`libpynix.command_name` gives the name a command has on the command line: the
class name in kebab case, unless the class sets `cli_name`.

## Take the three evaluation options

Every Nix CLI takes `--file`, `--flake` and `--attr`, and they mean the same
thing in each of them. Declare them once, on a base class of your own:

```{literalinclude} ../../libpynix/examples/evaluation_options_example.py
:language: python
```

A base class between `libpynix.Command` and your commands is also where a
program resolves a default from somewhere other than the command line. Declare
such an option with `opt(..., configured=True)`. `libpynix` records the mark
and reads nothing; the base class fills the attribute in. `pynix._settings`
is the one in this repository that does it, from the environment and from
`$XDG_CONFIG_HOME/pynix/config.toml`.

**A completer is a parameter, and this module supplies none.** Each of the
three takes `complete=`, and passes it through to `opt`. A program that gives
nothing keeps what these three did before, which is to let the shell offer file
names.

The reason the completer comes from outside is the reason this module can be
here at all. Answering a Tab after `--attr` means evaluating Nix while a person
holds a key down: it needs an evaluator, a budget, and a way to give up when
the budget runs out. A library that declares an option has none of the three.

`pynix` supplies them, and the shape is worth copying. `pynix._attr_completion`
holds the completer, and `pynix._nix_options` is the join: three wrappers, each
one calling the declaration of this module with the completer of that one. Its
command modules import the three from there rather than from `libpynix`, which
is one changed import line for each of them.

Read `pynix/src/pynix/_nix_options.py`. Its docstring gives the cost that makes
the split worth having: every import that reaches an evaluator sits inside the
function a completion calls, so a start that only lists an option pays for a
function object and nothing else.

**The walk itself is in `nanopynix_helpers.attr_completion`, and a second Nix
CLI takes it from there.** That module holds the two rules `nix` applies -- one
for `--file`, one for the fragment of a flake -- as two functions over a value
the caller already evaluated. It opens no store and holds no budget, so a
program keeps those decisions. It is not in `libpynix` for the reason this page
opens with: `libpynix` depends on `argcomplete` and on nothing else, and the
walk needs an evaluator.

`--flake` names which search its fragment resolves against, because each
subcommand of `nix` overrides the pair differently: `nix develop F#<TAB>`
offers what is under `devShells.<system>` and `nix build F#<TAB>` does not. So
`flake_option(search="dev-shell")` is what `pynix develop` declares, and the
name is resolved inside the completion rather than at import.

## Answer a shell completion

`complete(parser)` answers a completion and exits when the start is one, and
returns at once when it is not. `main` in `minimal_example.py` above is the
whole of it: build the parser, call `complete`, then dispatch.

**It imports `argcomplete` only when a shell asks.** The library is 39
modules, and the generated completion script is the only thing that sets the
`_ARGCOMPLETE` variable it reads. A command a person typed loads none of it.

`complete` also corrects one thing before it answers. argcomplete lexes the
line with a vendored `shlex` whose `commenters` is `#`, so everything from the
first `#` was dropped and `tool build --file .#hello --at<TAB>` completed an
empty word. A command line is not a script, and no part of one is a comment. A
flake reference is the shape a Nix program is typed with most, so this is not
a corner. Issue #221.

## Install the completion scripts

`nix/mk-app.nix` in this repository renders and installs the scripts for
`bash`, `zsh` and `fish`. Pass `completions = true`:

```nix
mkApp {
  name = "my-tool";
  inherit pythonSet;
  completions = true;
}
```

Nothing is read out of the command tree to do it. The script is the same for
every argcomplete program: it exports the variables the protocol names and
calls the program back on file descriptor 8. `nix/render-completions.py` says
where the script comes from, and `easykubenix` consumes the same function.

## Keep the start cheap

`pynix` loads 109 modules in a release build, down from 866. Two rules keep it
there, and a program built on this library needs both:

- **A command module holds its options, and not its body.** The parser loads
  every subcommand module on every start, so whatever `run` needs belongs in
  another module that only `run` imports. `pynix._impl` is that half.
- **A declaration lives away from the code that reads it.** `--attr` is a
  string until something resolves it, and resolving it means an evaluator.
  `libpynix.nix_options` declares the three options, and `pynix.target` reads
  them; that is 101 ms a start which evaluates nothing does not pay.

Issue #123 measured both, and `tests/meta/test_import_budget.py` is what keeps
them true.
