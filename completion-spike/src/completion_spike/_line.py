"""Read a half-typed command line, the way a completion has to see it.

**The whole command line is the input to a dynamic completion, and not the word
under the cursor.** What `--attr` may complete into depends on what the rest of
the line said: which engine, which file, which flake. A completion that
received only the prefix would answer for the default target and be wrong
whenever the user named another one.

Every shell can supply this. fish has `commandline -cp`, bash has `COMP_LINE`
with `COMP_POINT`, and zsh has `BUFFER` with `CURSOR`. `_layer` emits the right
one for each, and this module reads what arrives.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class Line:
    """One command line, cut at the cursor.

    ``words`` holds the finished words, without the name of the program.
    ``prefix`` is the unfinished word under the cursor, and is empty when the
    line ends with a space -- which is the commonest case of all, because a
    user presses Tab straight after typing an option.

    ``completing`` names the option whose value the cursor sits in, and is
    ``None`` when the cursor sits somewhere else. **Both spellings give the
    same answer**: after ``--attr hel`` and after ``--attr=hel``, ``completing``
    is ``--attr`` and ``prefix`` is ``hel``. The ``--attr=`` part is not a part
    of the value, and a candidate never starts with it.
    """

    words: tuple[str, ...]
    prefix: str
    completing: str | None = None

    def option(self, name: str, default: str) -> str:
        """The value that the line gives to *name*, or *default*.

        Both spellings are read: `--engine local` and `--engine=local`. The
        last one wins, because that is what the parser itself would do with a
        repeated option.
        """
        found = default
        for index, word in enumerate(self.words):
            if word == name and index + 1 < len(self.words):
                found = self.words[index + 1]
            elif word.startswith(f"{name}="):
                found = word.split("=", 1)[1]
        return found


def read_line(text: str) -> Line:
    """*text*, which is a command line up to the cursor, as a `Line`.

    **The line is the one input that every shell agrees on.** A shell splits
    the line into words by its own rules, and the rules differ: the default
    ``COMP_WORDBREAKS`` of bash holds ``=``, so bash makes three words of
    ``--attr=hel`` while fish and zsh make one. Reading the line here rather
    than the words of the shell gives one answer for all three.
    """
    try:
        words = shlex.split(text)
    except ValueError:
        # An unbalanced quote, which is what a half-typed `--attr "py` is. The
        # shell would not have split it either, so the plain split is no worse
        # and it keeps the completion answering instead of failing.
        words = text.split()
    prefix = "" if not text or text[-1].isspace() else (words.pop() if words else "")
    # The name of the program is not a word of the command line for this
    # purpose. It is always there, and nothing reads it.
    rest = tuple(words[1:])
    completing: str | None = None
    if prefix.startswith("-") and "=" in prefix:
        # `--attr=hel`. The option and the value are one word, so the value is
        # what follows the first `=`, and the option is what precedes it.
        completing, prefix = prefix.split("=", 1)
    elif rest and rest[-1].startswith("-"):
        # `--attr hel`, and `--attr ` with the cursor after the space.
        completing = rest[-1]
    return Line(words=rest, prefix=prefix, completing=completing)
