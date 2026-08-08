"""A derivation, and the ATerm text that Nix reads it from.

Nix stores a derivation as one line of ATerm:

.. code-block:: text

    Derive([outputs],[inputDrvs],[inputSrcs],system,builder,[args],[(k,v)...])

A planner writes that line into its own output. Nix reads the output back as a
derivation, because the output is text-hashed. Nothing else is involved: no
daemon, no ``nix`` binary, and no ``recursive-nix``.

Three rules govern what a planner may write, and each one comes from the store
rather than from this module:

1. Every store path that the derivation names must be valid in the store when
   Nix builds the derivation. A path that the planner had as an input is
   valid. A path that the planner invented is not.
2. Every store path that the derivation names must also be reachable from the
   producing derivation, because Nix scans the text-hashed output for
   references and refuses one it cannot account for.
3. A ``.drv`` file that the planner names in ``input_drvs`` must already be in
   the store. Nix writes a ``.drv`` file at instantiation, well before it
   builds anything, so a Nix expression can hand over a whole menu of
   unbuilt derivations. See :mod:`ddrn.menu`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from ._storepath import make_fixed_output_path

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: How Nix ingests an output. ``"text"`` is the one that makes an output a
#: derivation, which is what the ``dynamic-derivations`` feature reads back.
type IngestionMethod = Literal["nar", "flat", "text"]

#: The subset of :data:`IngestionMethod` that a *fixed* output may use. Nix
#: has no fixed text output outside its own internals.
type FixedMethod = Literal["nar", "flat"]

#: What Nix substitutes for the output path of a floating content-addressed
#: derivation. The value is a constant of Nix, and ``builtins.placeholder
#: "out"`` is where it comes from.
PLACEHOLDER_OUT = "/1rz4g4znpzjwh1xymhjpm42vipw92pr73vdgl6xs1hycac8kf2n9"

_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _string(text: str) -> str:
    return '"' + "".join(_ESCAPES.get(char, char) for char in text) + '"'


def _seq(items: Sequence[str]) -> str:
    return "[" + ",".join(items) + "]"


# Typed empty defaults. `default_factory=dict` gives a dataclass field whose
# type parameters a strict type checker cannot recover.
def _no_env() -> dict[str, str]:
    return {}


def _no_input_drvs() -> dict[str, Sequence[str]]:
    return {}


def _one_floating_output() -> dict[str, Output]:
    return {"out": Output()}


@dataclass(frozen=True, slots=True)
class Output:
    """One output of a derivation.

    ``method`` is how Nix ingests the output, and it decides what the output
    *is*:

    ``"nar"``
        A directory or a file, hashed as a NAR. The default, and what an
        ordinary build produces.
    ``"flat"``
        A single file, hashed as its bytes. What a download produces.
    ``"text"``
        A single file that Nix reads back **as a derivation**. This is the one
        that makes a derivation a planner. Use :meth:`plan`.

    Leave ``path`` and ``sha256`` unset for a floating output. Nix then picks
    the path after the build, and the planner needs no hash arithmetic. That
    is the shape to reach for first.
    """

    path: str | None = None
    sha256: str | None = None
    method: IngestionMethod = "nar"

    @classmethod
    def fixed(cls, name: str, *, sha256: str, method: FixedMethod = "flat", store_dir: str = "/nix/store") -> Output:
        """A fixed output, with its path computed to match its mode.

        Use this rather than :func:`ddrn.make_fixed_output_path` with a
        hand-written :class:`Output`. The path of a fixed output depends on
        the ingestion mode, Nix recomputes it and refuses a mismatch, and the
        message it prints names neither the mode nor the field that is wrong.

        ``method`` defaults to ``"flat"``, which is what a downloaded file is.
        Pass ``"nar"`` for a directory.
        """
        return cls(
            path=make_fixed_output_path(store_dir, name, sha256=sha256, recursive=method == "nar"),
            sha256=sha256,
            method=method,
        )

    @classmethod
    def plan(cls) -> Output:
        """The output of a planner: a floating derivation file.

        A derivation with this output is what ``builtins.outputOf`` names. An
        emitted derivation may carry it too, which makes the emitted
        derivation a planner in turn. See ``ddrn/examples/chain``.
        """
        return cls(method="text")


#: How Nix writes each ingestion method in the ATerm hash-algorithm field.
_ATERM_ALGO: dict[str, str] = {"nar": "r:sha256", "flat": "sha256", "text": "text:sha256"}

#: How Nix writes each ingestion method in `outputHashMode`.
_HASH_MODE: dict[str, str] = {"nar": "recursive", "flat": "flat", "text": "text"}


def _output_aterm(output: Output, name: str) -> str:
    algo = _ATERM_ALGO[output.method]
    return f"({_string(name)},{_string(output.path or '')},{_string(algo)},{_string(output.sha256 or '')})"


def _output_env(output: Output) -> dict[str, str]:
    mode = _HASH_MODE[output.method]
    if output.sha256 is None:
        return {"outputHashAlgo": "sha256", "outputHashMode": mode}
    return {"outputHash": output.sha256, "outputHashAlgo": "sha256", "outputHashMode": mode}


@dataclass(frozen=True, slots=True)
class Derivation:
    """A derivation that a planner emits.

    ``input_drvs`` maps the path of a ``.drv`` file to the output names that
    this derivation consumes. ``input_srcs`` holds plain store paths, which is
    where a tool that the planner already had as an input belongs.
    """

    name: str
    system: str
    builder: str
    args: Sequence[str] = ()
    env: Mapping[str, str] = field(default_factory=_no_env)
    outputs: Mapping[str, Output] = field(default_factory=_one_floating_output)
    input_srcs: Sequence[str] = ()
    input_drvs: Mapping[str, Sequence[str]] = field(default_factory=_no_input_drvs)

    def output_value(self, name: str) -> str:
        """What the ``$out``-style environment variable of ``name`` holds."""
        output = self.outputs[name]
        if output.path is not None:
            return output.path
        if name != "out":
            raise ValueError(
                f"{self.name}: only the 'out' output has a known placeholder; give {name!r} an explicit path"
            )
        return PLACEHOLDER_OUT

    def environment(self) -> dict[str, str]:
        """The full environment, including what Nix adds for the caller.

        Nix puts ``name``, ``system``, ``builder`` and every output into the
        environment of the builder. A derivation written by hand has to do the
        same, or the builder finds no ``$out``.
        """
        full: dict[str, str] = {
            "name": self.name,
            "system": self.system,
            "builder": self.builder,
            **self.env,
        }
        for output_name, output in self.outputs.items():
            full[output_name] = self.output_value(output_name)
            full.update(_output_env(output))
        return full

    def to_aterm(self) -> str:
        """The one line of ATerm text that Nix parses this derivation from.

        Every map is written in sorted order, which is the order that
        ``nix::Derivation::unparse`` writes, and therefore the order that keeps
        the text-hash of an unchanged plan stable.
        """
        outputs = _seq([_output_aterm(self.outputs[key], key) for key in sorted(self.outputs)])
        drvs = _seq(
            [
                f"({_string(path)},{_seq([_string(out) for out in sorted(self.input_drvs[path])])})"
                for path in sorted(self.input_drvs)
            ]
        )
        srcs = _seq([_string(path) for path in sorted(set(self.input_srcs))])
        args = _seq([_string(arg) for arg in self.args])
        env = _seq([f"({_string(key)},{_string(value)})" for key, value in sorted(self.environment().items())])
        return f"Derive({outputs},{drvs},{srcs},{_string(self.system)},{_string(self.builder)},{args},{env})"

    def referenced_paths(self, store_dir: str = "/nix/store") -> set[str]:
        """Every store path this derivation names, for a self-check.

        Rule 2 of the module docstring is the reason to call this. A planner
        can compare the result against the inputs that it received, and fail
        with a clear message instead of leaving Nix to report a reference that
        it cannot account for.
        """
        found = {*self.input_srcs, *self.input_drvs}
        for value in (self.builder, *self.args, *self.environment().values()):
            found.update(_scan_store_paths(value, store_dir))
        return {path for path in found if path.startswith(store_dir + "/")}


def _scan_store_paths(text: str, store_dir: str) -> set[str]:
    """Every store path root in ``text``, with any path below it removed.

    ``/nix/store/<hash>-bash/bin/bash`` reports the ``bash`` path, because the
    store tracks the root and not the file inside it.
    """
    prefix = store_dir + "/"
    found: set[str] = set()
    start = text.find(prefix)
    while start != -1:
        end = start + len(prefix)
        while end < len(text) and (text[end].isalnum() or text[end] in "+-._?="):
            end += 1
        found.add(text[start:end])
        start = text.find(prefix, end)
    return found
