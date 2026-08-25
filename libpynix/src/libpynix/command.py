"""The declaration of a command, and the argparse parser it becomes.

**A command class declares its options once, and the parser is built from it.**
Every ``run`` body in ``pynix._impl`` reads its options off an object --
``command.attr``, ``command.store``, ``command.explicit_options`` -- so a
decorator-driven port would declare all 61 options twice: once for the parser,
and once for a class that carries them into those thirteen modules. This layer
declares them once::

    class Build(Command):
        \"\"\"Build a Nix derivation value\"\"\"

        attr: str | None = opt(None, short="A", help="...")

        async def run(self) -> None:
            ...

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

**This module is a library, and it came out of `pynix`.** Issue #222 moved it:
`easykubenix` had copied the same 359 lines to get a second Nix CLI, and the
two diverged in six days. Nothing here knows about Nix. The three options that
every Nix CLI takes are in :mod:`libpynix.nix_options`, which declares them and
reads none of them.
"""

from __future__ import annotations

import argparse
import functools
import gettext
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
from typing import Any, ClassVar, override

from libpynix._typecheck import no_runtime_type_check

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
    #: True for an option the caller must name. Options only, and never
    #: together with `configured`: a configured option has a source below the
    #: command line, so requiring one at the parser would refuse a value the
    #: configuration file already gives.
    required: bool = False
    #: What answers a Tab after this option. `None` leaves it to the shell,
    #: which offers file names.
    complete: Completer | None = None


#: The default of a positional that the caller must give. Not `None`, which is
#: a legal default for an optional positional -- `pynix search` takes one.
MISSING: typing.Final = object()


@no_runtime_type_check  # a declaration returns a Spec, and the annotation names the value it will hold; beartype would flag every one
def opt(  # noqa: PLR0913 -- one keyword for each thing a declaration can say; collapsing them into a dict would only hide the names from the reader and from pyright
    default: Any = None,
    *,
    help: str,  # noqa: A002 -- argparse names the parameter `help`, and so did clypi
    short: str | None = None,
    negatable: bool = False,
    configured: bool = False,
    required: bool = False,
    complete: Completer | None = None,
) -> Any:
    """Declare an option.

    *configured* says that the default comes from somewhere else -- the
    environment, or a configuration file -- rather than from *default*. This
    layer only records the mark. The program that sets it is what resolves it,
    through a base class of its own between :class:`Command` and its commands:
    :func:`pynix._settings.option` passes the mark, and
    :class:`pynix._settings.ConfiguredCommand` reads it.
    """
    if required and configured:
        raise ValueError("an option cannot be both required and configured; see Spec.required")
    return Spec(
        default=default,
        help=help,
        short=short,
        negatable=negatable,
        configured=configured,
        required=required,
        complete=complete,
    )


@no_runtime_type_check  # see opt
def pos(*, help: str, default: Any = MISSING, complete: Completer | None = None) -> Any:  # noqa: A002 -- see opt
    """Declare a positional argument."""
    return Spec(default=default, help=help, positional=True, complete=complete)


class Command:
    """The base of every command.

    A subclass declares its options as annotated class attributes and gets an
    `__init__` that takes them as keyword arguments. `run` is what the command
    does, and `build_parser` is what makes it reachable.

    A program may put its own base between this class and its commands.
    `pynix._settings.ConfiguredCommand` is the one here that does: it resolves
    the options that `configured=True` marks, and it declares none of its own.
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


def _converted(annotation: Any) -> type | None:
    """The `type=` argparse needs for *annotation*, or `None` to leave it alone.

    **argparse hands back a string unless it is told otherwise.** Without this,
    `--limit 20` reached `rapidfuzz` as `"20"` and it answered `TypeError: an
    integer is required`, six frames away from the declaration that says `int`.

    A `list[Path]` answers `Path`: argparse applies `type` to each value it
    collects, so a repeated option and a repeated positional convert the same
    way a single one does.
    """
    inner = _element(annotation)
    return inner if inner in _CONVERTED else None


def _element(annotation: Any) -> Any:
    """*annotation* with `| None` and a `list[...]` wrapper taken off.

    argparse applies `type` and `choices` to each value it collects, so a
    repeated option and a single one answer the same question about what one
    value is.
    """
    inner = _unwrapped(annotation)
    if typing.get_origin(inner) is list:
        args = typing.get_args(inner)
        return _unwrapped(args[0]) if args else str
    return inner


def _choices(annotation: Any) -> tuple[Any, ...] | None:
    """The `choices=` argparse needs for *annotation*, or `None` for none.

    **A `Literal` says the set of words, so the parser checks it and the shell
    offers it.** Without this, a declaration wrote the words a second time in
    its help text and nothing checked them: `pynix` lists the eight verbosity
    names in prose and accepts any string. `easykubenix` reads
    `Literal["yaml11", "yaml12"]` and gets both for free.

    A `Literal` is not in `_CONVERTED`, so an option that has choices never
    also gets a `type`.
    """
    inner = _element(annotation)
    return typing.get_args(inner) if typing.get_origin(inner) is typing.Literal else None


def option_flags(field: str, spec: Spec) -> list[str]:
    """The flag spellings of *field*, longest first, as argparse wants them.

    Public, and for the same reason as `command_name`:
    `docs/_generate_pynix_reference.py` has to spell an option the same way the
    parser does. It spelled one itself until this function grew the rule below,
    and then rendered `--from-` for a flag the parser calls `--from`.

    **One trailing underscore is dropped, so a flag can carry a name that
    Python reserves.** `--from` is the name `nix copy` gives its source store,
    and `from` is a keyword, so no class attribute can hold it. A field called
    `from_` spells the flag `--from` and keeps the attribute `from_`, which is
    the convention PEP 8 already gives for this case. `--class`, `--import` and
    `--lambda` are the same problem in another program.

    `_add` passes `dest=field` for every option, so the attribute keeps the
    underscore that the flag drops. Without that, argparse would name the
    attribute after the flag and `Command.__init__` would never see the value.
    """
    flags = ["--" + field.removesuffix("_").replace("_", "-")]
    if spec.short is not None:
        flags.append("-" + spec.short.lstrip("-"))
    return flags


def _positional_kwargs(spec: Spec, *, repeated: bool) -> dict[str, Any]:
    """What argparse needs to know about a positional, beyond its type."""
    if repeated:
        return {"nargs": "*"}
    if spec.default is not MISSING:
        return {"nargs": "?", "default": argparse.SUPPRESS}
    return {}


def _option_kwargs(field: str, spec: Spec, inner: Any, *, repeated: bool) -> dict[str, Any]:
    """The same, for an option.

    **`SUPPRESS`, for every one.** An option the caller did not name is then
    absent from the namespace, and `Command.__init__` fills it from the
    declaration. That is one rule for the ordinary options and the
    configuration-backed ones together.
    """
    # `dest`, so that the attribute keeps the name the class declared. argparse
    # otherwise reads it off the first long flag, and `_flags` drops a trailing
    # underscore from that flag. See `_flags`.
    kwargs: dict[str, Any] = {"default": argparse.SUPPRESS, "dest": field}
    if spec.negatable:
        kwargs["action"] = argparse.BooleanOptionalAction
    elif inner is bool:
        kwargs["action"] = "store_true"
    elif repeated:
        kwargs["action"] = "append"
    if spec.required:
        kwargs["required"] = True
    return kwargs


def _add(parser: argparse.ArgumentParser, field: str, spec: Spec, annotation: Any) -> None:
    """Add one declared option or positional to *parser*.

    **`type` and `choices` are decided once, above the split.** A positional
    got neither until issue #222 made this layer a library: every `pos()` in
    `pynix` is a `str`, so the fault was latent there, and `easykubenix` has
    three `Path` positionals and hit it.

    No guard for a flag on either one, and none is needed: `bool` is in
    neither `_CONVERTED` nor a `Literal`, so both helpers answer `None` for
    one. A `type` beside `store_true` or `BooleanOptionalAction` is an
    argparse error, and `test_a_flag_is_not_given_a_type` keeps that true.
    """
    inner = _unwrapped(annotation)
    repeated = typing.get_origin(inner) is list

    kwargs: dict[str, Any] = {"help": spec.help}
    choices = _choices(annotation)
    if choices is not None:
        kwargs["choices"] = choices
    converted = _converted(annotation)
    if converted is not None:
        kwargs["type"] = converted

    if spec.positional:
        kwargs |= _positional_kwargs(spec, repeated=repeated)
        action = parser.add_argument(field, **kwargs)
    else:
        kwargs |= _option_kwargs(field, spec, inner, repeated=repeated)
        action = parser.add_argument(*option_flags(field, spec), **kwargs)
    # argcomplete reads this attribute off the action it did not create.
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


class _LazyParserMap(dict[str, argparse.ArgumentParser]):
    """A dictionary that builds a subparser on demand when looked up."""

    def __init__(self, action: _LazySubParsersAction) -> None:
        super().__init__()
        self._action = action

    def set_raw(self, key: str, value: argparse.ArgumentParser | None) -> None:
        if value is not None:
            super().__setitem__(key, value)
        else:
            super().__setitem__(key, None)  # type: ignore[arg-type] -- placeholder until materialized

    def get_raw(self, key: str) -> argparse.ArgumentParser | None:
        return super().get(key, None)

    @override
    def __getitem__(self, key: str) -> argparse.ArgumentParser:
        self._action.materialize(key)
        return super().__getitem__(key)

    @override
    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default


class _LazySubParsersAction(argparse._SubParsersAction):  # type: ignore[reportPrivateUsage, type-arg] -- subclassing standard subparser action for on-demand subparser instantiation  # noqa: SLF001
    """Subparsers action that builds child parsers on demand."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[reportUnknownMemberType] -- argparse type stubs
        self._lazy_map: _LazyParserMap = _LazyParserMap(self)
        self._name_parser_map = self._lazy_map
        self.choices = self._lazy_map
        self._factories: dict[str, tuple[Callable[[argparse.ArgumentParser], None], dict[str, Any]]] = {}

    def add_lazy_parser(
        self,
        name: str,
        *,
        help: str,  # noqa: A002 -- argparse names the parameter `help`
        factory: Callable[[argparse.ArgumentParser], None],
        **kwargs: Any,
    ) -> None:
        choice_action = self._ChoicesPseudoAction(name, (), help)  # type: ignore[reportUnknownMemberType, reportPrivateUsage] -- argparse _ChoicesPseudoAction
        self._choices_actions.append(choice_action)
        self._factories[name] = (factory, kwargs)
        self._lazy_map.set_raw(name, None)

    def materialize(self, name: str) -> argparse.ArgumentParser | None:
        parser = self._lazy_map.get_raw(name)
        if parser is None and name in self._factories:
            factory, kwargs = self._factories[name]
            if kwargs.get("prog") is None:
                kwargs["prog"] = f"{self._prog_prefix} {name}"
            color = getattr(self, "_color", None)
            if kwargs.get("color") is None and color is not None:
                kwargs["color"] = color
            parser_class = getattr(self, "_parser_class", argparse.ArgumentParser)
            created = parser_class(**kwargs)
            factory(created)
            self._lazy_map.set_raw(name, created)
            return created
        return parser


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
    sub = parser.add_subparsers(metavar="COMMAND", action=_LazySubParsersAction)
    if not isinstance(sub, _LazySubParsersAction):
        raise TypeError(f"expected _LazySubParsersAction, got {type(sub)}")
    for child in command.subcommands:
        sub.add_lazy_parser(
            command_name(child),
            help=_describe(child).partition("\n")[0],
            factory=functools.partial(_configure, command=child),
            description=_describe(child),
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

    _let_a_hash_stay_in_the_line()
    argcomplete.autocomplete(parser, exclude=_NOT_OFFERED)


def _let_a_hash_stay_in_the_line() -> None:
    """Stop argcomplete reading a `#` as the start of a comment.

    **A command line is not a script, and no part of one is a comment.**
    argcomplete lexes the line with a vendored `shlex` whose `commenters` is
    `#`, and `argcomplete.lexers.split_line` never clears it. So everything
    from the first `#` is dropped, and `pynix build --file .#hello --at<TAB>`
    completed an empty word and offered every option of `pynix build`:

        >>> split_line("pynix build --file .#hello --at", 31)
        ('', '', '', ['pynix', 'build', '--file', '.'], None)

    No shell reads it that way. bash treats `#` as a comment only at the start
    of a word, `fish -c 'echo a#b'` prints `a#b`, and `#` is in no
    `COMP_WORDBREAKS`. A flake reference is the shape a Nix program is typed
    with most, so this is not a corner. Issue #221.

    **Here, and not as a patch of the package.** The patch was one
    substitution and it was measured to cost far more: `argcomplete` is a
    dependency of `datamodel-code-generator`, whose test closure is
    `httpx2`, `elasticsearch`, `ipython`, `prance` and more. Overriding
    `argcomplete` rebuilt every one of them, and `httpx2` fails its own suite
    on macOS. This is the cheapest place that reaches only this program.

    **TODO: report this upstream, and then delete these lines.** The fix
    there is one line in `split_line` itself::

        lexer.commenters = ""

    No upstream issue tracks it. The nearest is kislyuk/argcomplete#362, which
    is about the vendored copies in general and not about this. Until somebody
    files it, every program that completes a flake reference has to install
    this correction for itself, and each of them meets the defect first.

    `split_line` builds the lexer itself, so there is no argument to pass. It
    reads the class off the module at call time, which is what makes a
    subclass enough.
    """
    from argcomplete.packages import _shlex  # noqa: PLC0415 -- only a completion reaches this

    class _Uncommented(_shlex.shlex):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            # The suppression sits on the call below, because the vendored
            # `shlex` of argcomplete carries no annotations at all.
            super().__init__(*args, **kwargs)  # type: ignore[reportUnknownMemberType] -- vendored shlex, no annotations
            self.commenters = ""

    # `lexers.py` binds this same module object, so the attribute reaches it.
    _shlex.shlex = _Uncommented


def _speed_up_gettext() -> None:
    """Cache translation misses so argparse does not repeat disk searches.

    **dgettext searches the disk on every call when no .mo file exists.**
    argparse calls gettext.dgettext for every standard section title and usage
    phrase across every subparser. Because standard gettext catches OSError and
    returns the message without caching missing translations in _translations,
    each parser construction searches locale directories repeatedly via
    hundreds of filesystem checks.

    Caching the NullTranslations fallback per (domain, localedir) eliminates
    the repeated filesystem searches while preserving real translations if
    present. Issue #240.
    """
    null = gettext.NullTranslations()
    cache: dict[tuple[str, str | None], Any] = {}

    def cached_dgettext(domain: str, message: str) -> str:
        localedir = getattr(gettext, "_localedirs", {}).get(domain, None)
        key = (domain, localedir)
        trans = cache.get(key)
        if trans is None:
            try:
                trans = gettext.translation(domain, localedir)
            except OSError:
                trans = null
            cache[key] = trans
        return trans.gettext(message)

    gettext.dgettext = cached_dgettext


_speed_up_gettext()


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
