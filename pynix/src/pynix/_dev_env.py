"""The build environment that ``get-env.sh`` dumps, and the bash that restores it.

``pynix develop`` and ``pynix print-dev-env`` both end here. The store side --
rewriting a derivation so that its builder dumps its own environment -- is
:meth:`nanopynix.protocols.AsyncStore.write_dev_shell_derivation`. This module
reads what that build wrote, and turns it back into bash.

Nix does the same two jobs in ``src/nix/develop.cc``: ``BuildEnvironment`` at
line 41, and ``makeRcScript`` at line 348. Each function below names the part
it mirrors, because the two have to agree for ``print-dev-env`` to agree with
``nix print-dev-env``.

``get-env.sh`` is Nix's own ``src/nix/get-env.sh``, carried by
``nanopynix-bindings`` because Nix compiles it into the ``nix`` binary and no
library carries it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from nanopynix import get_env_sh_path

#: The ``get-env.sh`` that ``nanopynix-bindings`` ships -- Nix's own
#: ``src/nix/get-env.sh``, carried there because Nix compiles it into the
#: ``nix`` binary and no library carries it. See
#: ``nanopynix_bindings._get_env`` and ``nanopynix.get_env`` for the
#: provenance.
GET_ENV_SH: Path = get_env_sh_path()

#: Variables that the shell must keep from the caller rather than take from the
#: build. ``develop.cc:315``, unchanged: `HOME` and `TERM` make a shell usable,
#: and `NIX_BUILD_TOP` and the temp variables are set again further down.
IGNORED_VARIABLES: frozenset[str] = frozenset(
    {
        "BASHOPTS",
        "HOME",
        "NIX_BUILD_TOP",
        "NIX_ENFORCE_PURITY",
        "NIX_LOG_FD",
        "NIX_REMOTE",
        "PPID",
        "SHELLOPTS",
        "SSL_CERT_FILE",
        "TEMP",
        "TEMPDIR",
        "TERM",
        "TMP",
        "TMPDIR",
        "TZ",
        "UID",
    }
)

#: Colon-separated variables that are prepended to the caller's, rather than
#: replacing them. ``develop.cc:356`` asks that this list stay short: every
#: entry is a hole in the purity of the environment. `PATH` is what makes the
#: shell able to run anything at all, and `XDG_DATA_DIRS` is what makes
#: completion load.
_PREPENDED_VARIABLES = ("PATH", "XDG_DATA_DIRS")

_TEMP_VARIABLES = ("TMP", "TMPDIR", "TEMP", "TEMPDIR")

VariableKind = Literal["exported", "var", "array", "associative", "unknown"]


class DevEnvError(RuntimeError):
    """The environment JSON is not the shape ``get-env.sh`` writes."""


def quote(word: str) -> str:
    """Single-quote *word* for bash, mirroring ``escapeShellArgAlways``.

    Not :func:`shlex.quote`, which leaves a word that needs no quoting alone.
    Nix quotes every word, so the rendered script differs from ``nix
    print-dev-env`` on almost every line if this one does not.
    """
    escaped = word.replace("'", "'\\''")
    return f"'{escaped}'"


def _no_bash_functions() -> dict[str, str]:
    """Default for ``bash_functions``. A named function rather than ``dict``,
    which would give the field an unparameterised ``dict[Unknown, Unknown]``."""
    return {}


@dataclass(frozen=True)
class Variable:
    """One shell variable, with the kind ``declare -p`` reported for it."""

    kind: VariableKind
    value: str | list[str] | dict[str, str] | None


@dataclass(frozen=True)
class BuildEnvironment:
    """The environment of one derivation, as ``get-env.sh`` wrote it.

    Mirrors ``BuildEnvironment`` in ``develop.cc:41``.
    """

    variables: dict[str, Variable]
    bash_functions: dict[str, str] = field(default_factory=_no_bash_functions)
    structured_attrs: dict[str, str] | None = None

    @classmethod
    def from_json(cls, raw: str) -> BuildEnvironment:
        """Parse the JSON that the rewritten derivation's builder wrote."""
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DevEnvError(f"the build environment is not valid JSON: {exc}") from exc
        if not isinstance(document, dict):
            raise DevEnvError(f"the build environment must be an object, got {type(document).__name__}")
        parsed = cast("dict[str, Any]", document)

        variables: dict[str, Variable] = {}
        for name, entry in _mapping(parsed.get("variables", {}), "variables").items():
            if not isinstance(entry, dict):
                raise DevEnvError(f"variable {name!r} must be an object, got {type(entry).__name__}")
            fields = cast("dict[str, Any]", entry)
            # `unknown` is what the script writes for `-i`, `-r` and `-n`,
            # which it does not handle. Kept rather than dropped, so that
            # `to_bash` can skip it deliberately.
            kind = cast("VariableKind", str(fields.get("type", "unknown")))
            variables[name] = Variable(kind=kind, value=fields.get("value"))

        structured = parsed.get("structuredAttrs")
        return cls(
            variables=variables,
            bash_functions={
                name: str(body) for name, body in _mapping(parsed.get("bashFunctions", {}), "bashFunctions").items()
            },
            structured_attrs=(
                {name: str(body) for name, body in _mapping(structured, "structuredAttrs").items()}
                if structured is not None
                else None
            ),
        )

    @property
    def provides_structured_attrs(self) -> bool:
        return self.structured_attrs is not None

    def output_names(self) -> list[str]:
        """The names of the derivation's outputs, whichever way they arrived.

        A structured-attrs derivation makes ``outputs`` an associative array of
        name to path; otherwise it is a space-separated list of *variable*
        names, each of which holds its own path. ``develop.cc:388`` reads both.
        """
        outputs = self.variables.get("outputs")
        if outputs is None:
            raise DevEnvError("the build environment has no 'outputs' variable")
        if self.provides_structured_attrs:
            if not isinstance(outputs.value, dict):
                raise DevEnvError("'outputs' must be an associative array in a structured-attrs derivation")
            return list(outputs.value)
        if not isinstance(outputs.value, str):
            raise DevEnvError("'outputs' must be a string")
        return outputs.value.split()

    def output_paths(self) -> dict[str, str]:
        """Map each output name to the store path the build environment gives."""
        outputs = self.variables["outputs"]
        if self.provides_structured_attrs:
            return dict(cast("dict[str, str]", outputs.value))
        paths: dict[str, str] = {}
        for name in self.output_names():
            variable = self.variables.get(name)
            if variable is None or not isinstance(variable.value, str):
                raise DevEnvError(f"output {name!r} has no path in the build environment")
            paths[name] = variable.value
        return paths

    def to_json(self) -> dict[str, Any]:
        """Render the environment back to JSON, mirroring ``develop.cc:100``.

        Not the text that arrived: a variable of an unhandled kind is dropped
        here, exactly as ``fromJSON`` drops it, so what ``print-dev-env
        --json`` prints is what a reader can act on.
        """
        variables: dict[str, Any] = {}
        for name, variable in self.variables.items():
            if variable.kind == "unknown":
                continue
            variables[name] = {"type": variable.kind, "value": variable.value}
        document: dict[str, Any] = {
            "variables": variables,
            "bashFunctions": dict(self.bash_functions),
        }
        if self.structured_attrs is not None:
            document["structuredAttrs"] = {
                ".attrs.sh": self.structured_attrs.get(".attrs.sh", ""),
                ".attrs.json": self.structured_attrs.get(".attrs.json", ""),
            }
        return document

    def to_bash(self, ignore: frozenset[str] = IGNORED_VARIABLES) -> str:
        """Render the environment as bash, mirroring ``develop.cc:151``."""
        lines: list[str] = []
        for name, variable in self.variables.items():
            if name in ignore:
                continue
            match variable.kind:
                case "exported" | "var":
                    if not isinstance(variable.value, str):
                        raise DevEnvError(f"variable {name!r} is {variable.kind} but its value is not a string")
                    lines.append(f"{name}={quote(variable.value)}")
                    if variable.kind == "exported":
                        lines.append(f"export {name}")
                case "array":
                    if not isinstance(variable.value, list):
                        raise DevEnvError(f"variable {name!r} is an array but its value is not a list")
                    items = "".join(f"{quote(item)} " for item in variable.value)
                    lines.append(f"declare -a {name}=({items})")
                case "associative":
                    if not isinstance(variable.value, dict):
                        raise DevEnvError(f"variable {name!r} is associative but its value is not an object")
                    pairs = "".join(f"[{quote(key)}]={quote(item)} " for key, item in variable.value.items())
                    lines.append(f"declare -A {name}=({pairs})")
                case _:
                    # `unknown`, which the script writes for the declare flags
                    # it does not handle. Nix drops these too.
                    continue

        lines.extend(f"{name} ()\n{{\n{body}}}" for name, body in self.bash_functions.items())
        return _render(lines)


def make_rc_script(
    environment: BuildEnvironment,
    *,
    outputs_dir: Path,
    ignore: frozenset[str] = IGNORED_VARIABLES,
) -> str:
    """Compose the bash that restores *environment*, mirroring ``develop.cc:348``.

    *outputs_dir* replaces each real output path in the script. A dev shell has
    not built its outputs, so a variable that names one would otherwise point
    at a path that does not exist.
    """
    # The saved variables are read before the environment overwrites them and
    # appended after, so the order of these three parts is the whole point.
    prologue = ["unset shellHook"]
    for name in _PREPENDED_VARIABLES:
        prologue.append(f"{name}=${{{name}:-}}")
        prologue.append(f'nix_saved_{name}="${name}"')

    epilogue = [f'{name}="${name}${{nix_saved_{name}:+:$nix_saved_{name}}}"' for name in _PREPENDED_VARIABLES]
    epilogue.append('export NIX_BUILD_TOP="$(mktemp -d -t nix-shell.XXXXXX)"')
    epilogue.extend(f'export {name}="$NIX_BUILD_TOP"' for name in _TEMP_VARIABLES)
    epilogue.append('eval "${shellHook:-}"')

    script = _render(prologue) + environment.to_bash(ignore) + _render(epilogue)

    # The rewrite is last, and it runs over the whole script rather than over
    # the values alone: an output path also appears inside a bash function
    # body, which `to_bash` has already rendered as text by this point. Nix
    # does the same, at `develop.cc:383`.
    for name, path in environment.output_paths().items():
        script = script.replace(path, str(outputs_dir / name))
    return script


def _render(lines: list[str]) -> str:
    return "".join(f"{line}\n" for line in lines)


def _mapping(value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DevEnvError(f"{what} must be an object, got {type(value).__name__}")
    return cast("dict[str, Any]", value)
