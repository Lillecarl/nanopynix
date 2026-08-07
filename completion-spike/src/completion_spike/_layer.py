"""Shell code that adds a dynamic candidate to a static cyclopts script.

**The static script is not edited.** That is the property this module exists to
establish, because a generator's output is not a stable interface: cyclopts
would move a line and quietly break any code that rewrote one. Each renderer
here therefore uses the extension point that its own shell already has:

- **fish** appends. ``complete`` is additive, so a second line for the same
  option adds candidates to the first one rather than replacing it.
- **bash** and **zsh** wrap. Each generates one function, so the layer
  registers a function of its own that answers the dynamic case and delegates
  every other case to the generated one.

A wrapper reads the word before the cursor to decide. It does not reach into
the generated function, and it does not depend on how that function computes
the command path.

**The name of the generated function is read out of the generated script**, and
not guessed. cyclopts calls it ``_cyclopts_demo`` in zsh and ``_demo`` in bash,
and a guess that was right for one was wrong for the other. `entry_point` below
is the single narrow coupling to cyclopts here, and it fails loudly rather than
writing a script that calls a function that does not exist.

Known gap, and it belongs to the spike rather than to this code: ``--attr=x``
is one word in fish and zsh but three in bash, whose default
``COMP_WORDBREAKS`` holds ``=``. The renderers below handle the separate-word
form, which is what a shell produces when the user presses Tab after a space.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

#: The shells that this module writes, and that `cyclopts` generates for.
#:
#: A `Literal` and not `str`, because `cyclopts.App.generate_completion` takes
#: exactly these three and a plain `str` does not satisfy it. Naming the type
#: here means the check happens at every call site rather than at that one.
type Shell = Literal["fish", "bash", "zsh"]

SHELLS: tuple[Shell, ...] = ("fish", "bash", "zsh")

#: The function that bash is told to complete with, in the generated script.
_BASH_ENTRY_POINT = re.compile(r"^complete\s+-F\s+(\S+)\s", re.MULTILINE)

#: The function that the generated zsh script defines. zsh registers nothing
#: itself: the script carries a `#compdef` line and expects to be autoloaded
#: from `fpath` under the name of the program. Sourcing it therefore defines
#: the function and binds nothing, and the layer below supplies the binding.
_ZSH_ENTRY_POINT = re.compile(r"^(_\S+)\(\)\s*\{", re.MULTILINE)


@dataclass(frozen=True)
class DynamicValue:
    """One option whose value the program itself completes.

    ``command_path`` is empty for an option of the root command. ``subcommand``
    names the command that writes the candidates, one for each line.
    """

    command_path: tuple[str, ...]
    option: str
    subcommand: str


def entry_point(shell: Shell | str, static: str) -> str:
    """The name of the completion function that *static* defines."""
    pattern = {"bash": _BASH_ENTRY_POINT, "zsh": _ZSH_ENTRY_POINT}.get(shell)
    if pattern is None:
        raise ValueError(f"{shell} has no completion function to wrap")
    found = pattern.search(static)
    if found is None:
        raise ValueError(f"found no completion function in the generated {shell} script")
    return found.group(1)


def _quote(text: str) -> str:
    """Escape *text* for a single-quoted shell string."""
    return text.replace("\\", "\\\\").replace("'", r"\'")


def _fish(program: str, values: tuple[DynamicValue, ...], _static: str) -> list[str]:
    """One ``complete`` line for each dynamic option.

    ``-r`` repeats what cyclopts already said about the option, because fish
    reads each ``complete`` line on its own: without it this line would offer
    the candidates as arguments of the command rather than of the option.

    ``-f`` stops fish adding the files of the working directory to the menu,
    which it does by default and which would bury the candidates.

    The command substitution is inside single quotes on purpose. fish evaluates
    the argument of ``-a`` when it completes, and not when it reads this file.

    ``commandline -cp`` is the whole line up to the cursor, and it carries **no
    quotes**. Two measurements decide that, and both go against the habit that
    bash and zsh teach:

    - Quoting it breaks it. fish does not expand a command substitution nested
      inside double quotes here: with ``--line "(commandline -cp)"`` the
      program received the literal string ``(commandline -cp)``, and offered
      nothing at all.
    - Quoting it is not needed. fish splits the output of a command
      substitution on newlines and on nothing else, so a line full of spaces
      arrives as one argument, with its trailing space intact.
    """
    lines: list[str] = []
    for value in values:
        condition = (
            f"__fish_{program}_using_command {' '.join(value.command_path)}"
            if value.command_path
            else "__fish_use_subcommand"
        )
        candidates = f"({program} {value.subcommand} --line (commandline -cp))"
        lines.append(
            f"complete -c {program} -n '{_quote(condition)}'"
            f" -l {_quote(value.option.lstrip('-'))} -r -f -a '{_quote(candidates)}'"
        )
    return lines


def _bash(program: str, values: tuple[DynamicValue, ...], static: str) -> list[str]:
    """A wrapper function, registered in place of the generated one.

    ``mapfile`` and not ``COMPREPLY=( $(...) )``: the second form splits on
    every space and then globs the result, so a candidate that holds a space or
    a ``*`` would be destroyed. A candidate is one line, and ``mapfile`` reads
    lines.

    ``${COMP_LINE:0:$COMP_POINT}`` is the line up to the cursor. bash gives the
    whole line in ``COMP_LINE`` and the cursor in ``COMP_POINT``, and the part
    after the cursor is not context for a completion.
    """
    generated = entry_point("bash", static)
    wrapper = f"{generated}_dynamic"
    lines = [
        f"{wrapper}() {{",
        # Only the word before the cursor, which is what decides *whether* to
        # answer. What to answer comes from the whole line, below.
        "    local prev",
        '    prev="${COMP_WORDS[COMP_CWORD-1]}"',
    ]
    for value in values:
        guard = f'[[ "$prev" == "{value.option}" ]]'
        for depth, word in enumerate(value.command_path, start=1):
            # The path words come in order, and each one sits at its own depth,
            # so this is an exact test of the command path and not a search for
            # the word anywhere on the line.
            guard += f' && [[ "${{COMP_WORDS[{depth}]}}" == "{word}" ]]'
        lines += [
            f"    if {guard}; then",
            f'        mapfile -t COMPREPLY < <({program} {value.subcommand} --line "${{COMP_LINE:0:$COMP_POINT}}")',
            "        return",
            "    fi",
        ]
    lines += [
        f"    {generated}",
        "}",
        "",
        f"complete -F {wrapper} {program}",
    ]
    return lines


def _zsh(program: str, values: tuple[DynamicValue, ...], static: str) -> list[str]:
    """A wrapper function, bound with ``compdef``.

    ``${(f)...}`` splits the output on newlines and on nothing else, which is
    the zsh spelling of the same rule that ``mapfile`` gives bash.

    ``${BUFFER[1,CURSOR]}`` is the line up to the cursor. `BUFFER` and `CURSOR`
    belong to the line editor, and a completion function runs inside it, so
    both are readable here.

    The delegation passes ``"$@"`` on, because zsh calls a completion function
    with the arguments of the context and the generated function reads them.
    """
    generated = entry_point("zsh", static)
    wrapper = f"{generated}_dynamic"
    lines = [
        f"{wrapper}() {{",
        "    local prev",
        '    prev="${words[CURRENT-1]}"',
    ]
    for value in values:
        guard = f'[[ "$prev" == "{value.option}" ]]'
        for depth, word in enumerate(value.command_path, start=2):
            # `words[1]` is the program itself in zsh, so the path starts at 2.
            guard += f' && [[ "${{words[{depth}]}}" == "{word}" ]]'
        lines += [
            f"    if {guard}; then",
            "        local -a candidates",
            f'        candidates=( ${{(f)"$({program} {value.subcommand} --line "${{BUFFER[1,CURSOR]}}")"}} )',
            "        compadd -a candidates",
            "        return",
            "    fi",
        ]
    lines += [
        f'    {generated} "$@"',
        "}",
        "",
        f"compdef {wrapper} {program}",
    ]
    return lines


_RENDERERS = {"fish": _fish, "bash": _bash, "zsh": _zsh}

_HEADER = "# The dynamic half, from completion_spike._layer. It rests on the static half above."


def render_layer(shell: Shell | str, program: str, values: tuple[DynamicValue, ...], static: str) -> str:
    """The dynamic half of the completion script of *program*, for *shell*.

    *static* is the script that cyclopts generated. It is read, and never
    changed: `entry_point` takes the name of the function to delegate to out of
    it, and fish needs nothing from it at all.
    """
    renderer = _RENDERERS.get(shell)
    if renderer is None:
        raise ValueError(f"no dynamic layer for {shell!r}; expected one of {', '.join(SHELLS)}")
    return "\n".join([_HEADER, *renderer(program, values, static)]) + "\n"


def render_script(shell: Shell | str, program: str, values: tuple[DynamicValue, ...], static: str) -> str:
    """The whole completion script: the static half, and then the dynamic one.

    One file, because a shell loads one file.
    """
    return static + "\n" + render_layer(shell, program, values, static)
