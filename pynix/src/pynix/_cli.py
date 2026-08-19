"""The declaration of a `pynix` command, and the argparse parser it becomes.

**A command class declares its options once, and the parser is built from it.**
Every `run` body in `pynix._impl` reads its options off an object --
`command.attr`, `command.store`, `command.explicit_options` -- so a
decorator-driven port would declare all 61 options twice: once for the parser,
and once for a class that carries them into those thirteen modules. This layer
declares them once. The shape of a command module does not change::

    class Build(ConfiguredCommand):
        \"\"\"Build a Nix derivation value\"\"\"

        attr: str | None = opt(None, short="A", help="...")

        async def run(self) -> None:
            await _impl.build.run_build(self)

**argparse parses, and argcomplete completes.** Issue #214 measured every
candidate on a pty, in fish, bash and zsh. argcomplete is the only one of them
that gets all nine lines of #213 right in all three shells, and the reason is
its protocol: the shell sends the raw command line and the cursor offset rather
than a list of words, and it sends bash's `COMP_WORDBREAKS` so that a value
holding `:` or `=` survives. `--store ssh://<TAB>` and `--attr=hel<TAB>` are
the shapes that decided it -- click answered neither in bash, and in zsh it
replaced `--attr=hel` with `hello` and lost the option.

**`argparse.SUPPRESS` is what says the caller named an option.** An option that
nobody named is absent from the namespace, so `explicit_options` is a set
membership test rather than a sentinel. clypi needed `UNSET` because it has no
equivalent, and that sentinel is gone with it.
"""

from __future__ import annotations

import argparse
import inspect
import os
import types
import typing

# A real import, and not a `TYPE_CHECKING` one. `NANOPYNIX_BEARTYPING=1` makes
# beartype resolve every annotation at run time, and a name that only the type
# checker can see becomes a forward reference that resolves to itself:
# "Forward reference proxy <forwardref Sequence> circularly references itself".
# `collections.abc` is already loaded by the interpreter, so this costs nothing.
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from nanopynix._typechecking import no_runtime_type_check

#: What argcomplete calls a completer: it is handed the text typed so far and
#: answers with the candidates that start with it.
type Completer = Callable[..., Sequence[str]]


@dataclass(frozen=True)
class Spec:
    """One declared option or positional, before argparse has seen it."""

    #: What the caller gets when they name neither the flag nor a source below
    #: it. `MISSING` for a positional that the caller must give.
    default: Any = None
    #: The help line. argparse wraps it, so it is written as one sentence.
    help: str = ""
    #: A one-letter alias, as `-A`. Options only.
    short: str | None = None
    #: True for a flag that also gets a `--no-` spelling.
    negatable: bool = False
    #: True for a positional argument.
    positional: bool = False
    #: True where the default comes from the environment or the configuration
    #: file rather than from `default` above. `pynix._settings` resolves it.
    configured: bool = False
    #: What answers a Tab after this option. `None` leaves it to the shell,
    #: which offers file names.
    complete: Completer | None = None


#: The default of a positional that the caller must give. Not `None`, which is
#: a legal default for an optional positional -- `pynix osearch` takes one.
MISSING: typing.Final = object()


@no_runtime_type_check  # a declaration returns a Spec, and the annotation names the value it will hold; beartype would flag every one
def opt(  # noqa: PLR0913 -- one keyword for each thing a declaration can say; collapsing them into a dict would only hide the names from the reader and from pyright
    default: Any = None,
    *,
    help: str,  # noqa: A002 -- argparse names the parameter `help`, and so did clypi
    short: str | None = None,
    negatable: bool = False,
    configured: bool = False,
    complete: Completer | None = None,
) -> Any:
    """Declare an option.

    *configured* says that the default comes from the environment or the
    configuration file rather than from *default*.
    :func:`pynix._settings.option` is what passes it, and
    :class:`pynix._settings.ConfiguredCommand` is what resolves it.
    """
    return Spec(
        default=default,
        help=help,
        short=short,
        negatable=negatable,
        configured=configured,
        complete=complete,
    )


@no_runtime_type_check  # see opt
def pos(*, help: str, default: Any = MISSING, complete: Completer | None = None) -> Any:  # noqa: A002 -- see opt
    """Declare a positional argument."""
    return Spec(default=default, help=help, positional=True, complete=complete)


class Command:
    """The base of every `pynix` command.

    A subclass declares its options as annotated class attributes and gets an
    `__init__` that takes them as keyword arguments. `run` is what the command
    does, and `build_parser` is what makes it reachable.
    """

    #: The name on the command line. Defaults to the class name in kebab case.
    #:
    #: **`cli_name`, and not `name`.** `pynix store add-path` declares an
    #: option called `--name`, and an option becomes a class attribute of the
    #: command that declares it. With this called `name`, that command's own
    #: declaration shadowed it and the subparser was registered under the repr
    #: of a `Spec`. `_RESERVED` below turns the next collision into an error.
    cli_name: ClassVar[str] = ""

    #: The commands mounted under this one. Empty for a leaf.
    subcommands: ClassVar[tuple[type[Command], ...]] = ()

    #: Filled by `__init_subclass__`, so a subclass of a subclass inherits the
    #: options of its base. `ConfiguredCommand` declares none of its own; a
    #: command that inherits from it does.
    specs: ClassVar[dict[str, Spec]] = {}

    #: The annotation of each declared name, resolved to an object. It decides
    #: whether an option is a flag, a repeated value or a path.
    types: ClassVar[dict[str, Any]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        specs: dict[str, Spec] = {}
        annotations: dict[str, Any] = {}
        for base in reversed(cls.__mro__):
            if base is object:
                continue
            resolved = inspect.get_annotations(base, eval_str=True)
            for field, value in vars(base).items():
                if isinstance(value, Spec):
                    specs[field] = value
                    annotations[field] = resolved.get(field, str)
        shadowed = sorted(specs.keys() & _RESERVED)
        if shadowed:
            raise TypeError(f"{cls.__name__} declares {shadowed}, which this class already uses for itself")
        cls.specs = specs
        cls.types = annotations

    def __init__(self, **values: Any) -> None:
        """Take what the caller named, and fill the rest from the declaration.

        Every option is declared with `argparse.SUPPRESS`, so *values* holds
        exactly what the caller typed. That is what `explicit_options` needs,
        and it is the whole reason the `UNSET` sentinel of clypi is gone.
        """
        for field, spec in type(self).specs.items():
            if field in values:
                setattr(self, field, values[field])
            elif spec.configured:
                # `pynix._settings.ConfiguredCommand` puts the resolved value
                # here. Until it does, the attribute has to exist.
                setattr(self, field, None)
            else:
                setattr(self, field, _declared_default(spec, type(self).types[field]))

    async def run(self) -> None:
        """What the command does. A group that only mounts others has none."""
        raise NotImplementedError(type(self).__name__)


#: The annotations that argparse has to be told about. Everything else it
#: leaves as the string the caller typed, which is what `str` wants anyway.
_CONVERTED: typing.Final = (int, float, Path)

#: The class attributes this layer owns. A command that declares an option of
#: the same name would shadow one of them, so `Command.__init_subclass__`
#: refuses instead.
_RESERVED: typing.Final = frozenset({"cli_name", "subcommands", "specs", "types", "run"})


def _declared_default(spec: Spec, annotation: Any) -> Any:
    """The value of an option the caller did not name.

    A repeated option gets a new list each time, because a shared one would be
    the default of every instance and would keep what a previous run appended.
    A `list` declares its own empty default, so a declaration never has to
    write `default_factory` for the one case where a literal `[]` would be
    wrong.
    """
    if spec.default in {None, MISSING} and typing.get_origin(_unwrapped(annotation)) is list:
        return []
    return None if spec.default is MISSING else spec.default


def _unwrapped(annotation: Any) -> Any:
    """*annotation* with `| None` taken off."""
    if typing.get_origin(annotation) in {typing.Union, types.UnionType}:
        parts = [part for part in typing.get_args(annotation) if part is not type(None)]
        if len(parts) == 1:
            return parts[0]
    return annotation


def _flags(field: str, spec: Spec) -> list[str]:
    """The flag spellings of *field*, longest first, as argparse wants them."""
    flags = ["--" + field.replace("_", "-")]
    if spec.short is not None:
        flags.append("-" + spec.short.lstrip("-"))
    return flags


def _add(parser: argparse.ArgumentParser, field: str, spec: Spec, annotation: Any) -> None:
    """Add one declared option or positional to *parser*."""
    inner = _unwrapped(annotation)
    repeated = typing.get_origin(inner) is list
    kwargs: dict[str, Any] = {"help": spec.help}

    if spec.positional:
        if repeated:
            kwargs["nargs"] = "*"
        elif spec.default is not MISSING:
            kwargs["nargs"] = "?"
            kwargs["default"] = argparse.SUPPRESS
        action = parser.add_argument(field, **kwargs)
        action.completer = spec.complete  # type: ignore[attr-defined] -- argcomplete reads this attribute off the action it did not create
        return

    # **`SUPPRESS`, for every option.** An option the caller did not name is
    # then absent from the namespace, and `Command.__init__` fills it from the
    # declaration. That is one rule for the ordinary options and the
    # configuration-backed ones together.
    kwargs["default"] = argparse.SUPPRESS
    if spec.negatable:
        kwargs["action"] = argparse.BooleanOptionalAction
    elif inner is bool:
        kwargs["action"] = "store_true"
    elif repeated:
        kwargs["action"] = "append"
    elif inner in _CONVERTED:
        # **argparse hands back a string unless it is told otherwise.** Without
        # this, `--limit 20` reached `rapidfuzz` as `"20"` and it answered
        # `TypeError: an integer is required`, six frames away from the
        # declaration that says `int`.
        kwargs["type"] = inner
    action = parser.add_argument(*_flags(field, spec), **kwargs)
    action.completer = spec.complete  # type: ignore[attr-defined] -- see above


def command_name(command: type[Command]) -> str:
    """The name of *command* on the command line.

    Public, because `docs/_generate_pynix_reference.py` has to spell a command
    the same way the parser does.
    """
    if command.cli_name:
        return command.cli_name
    head, *rest = command.__name__
    return head.lower() + "".join("-" + c.lower() if c.isupper() else c for c in rest)


def _describe(command: type[Command]) -> str:
    """The help text of *command*, which is its docstring."""
    return inspect.getdoc(command) or ""


def _configure(parser: argparse.ArgumentParser, command: type[Command]) -> None:
    """Put the options of *command* on *parser*, and mount what is under it."""
    for field, spec in command.specs.items():
        if not spec.positional:
            _add(parser, field, spec, command.types[field])
    for field, spec in command.specs.items():
        if spec.positional:
            _add(parser, field, spec, command.types[field])
    # `set_defaults`, so that the parser of the deepest command names the class
    # that runs. argparse hands the namespace back with no record of which
    # subparser filled it in.
    if not command.subcommands:
        parser.set_defaults(_command=command)
        return
    sub = parser.add_subparsers(metavar="COMMAND")
    for child in command.subcommands:
        _configure(
            sub.add_parser(
                command_name(child),
                help=_describe(child).partition("\n")[0],
                description=_describe(child),
            ),
            child,
        )


def build_parser(root: type[Command]) -> argparse.ArgumentParser:
    """*root*, and everything under it, as an argparse parser."""
    parser = argparse.ArgumentParser(prog=command_name(root), description=_describe(root))
    _configure(parser, root)
    return parser


#: Names that argcomplete must not offer. `-h` and `--help` are argparse's own
#: and are noise beside the real candidates.
_NOT_OFFERED: typing.Final = ("-h", "--help")


def complete(parser: argparse.ArgumentParser) -> None:
    """Answer a shell completion, when this start is one, and exit.

    **argcomplete is imported here and not at the top of the module.** It is 39
    modules, and a run that is not a completion needs none of them -- which is
    the whole subject of issue #123, and the reason
    `tests/meta/test_import_budget.py` counts what `import pynix` loads. The
    variable is set by the generated script and by nothing else, so the import
    happens on a completion keypress and never on a real command.
    """
    if not os.environ.get("_ARGCOMPLETE"):
        return
    import argcomplete  # noqa: PLC0415 -- 39 modules that only a completion needs; see this function's docstring

    argcomplete.autocomplete(parser, exclude=_NOT_OFFERED)


def dispatch(parser: argparse.ArgumentParser, namespace: argparse.Namespace) -> Command:
    """The command the caller named, built from what they typed.

    A caller who names a group and stops -- `pynix store` -- has named no
    command that runs, and argparse leaves `_command` unset. That is a request
    for help, and the help of the group is the answer.
    """
    values = {name: value for name, value in vars(namespace).items() if not name.startswith("_")}
    command = getattr(namespace, "_command", None)
    if command is None:
        parser.print_help()
        raise SystemExit(0)
    return command(**values)


def group(name: str, *, help: str, subcommands: Sequence[type[Command]]) -> type[Command]:  # noqa: A002 -- see opt
    """A command that only mounts others, declared in one expression."""
    return type(
        name.replace("-", " ").title().replace(" ", ""),
        (Command,),
        {"cli_name": name, "__doc__": help, "subcommands": tuple(subcommands)},
    )
