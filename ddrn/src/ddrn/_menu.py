"""The menu that a Nix expression hands to a planner.

A planner decides *which* derivations to build. It does not have to invent
them. The Nix expression instantiates every candidate, which costs no build,
and passes the resulting ``.drv`` paths across as data. The planner then names
the subset it wants, and Nix builds that subset and nothing else.

Two Nix builtins make this safe:

``builtins.unsafeDiscardOutputDependency``
    Turns a ``drvPath`` into a plain string. The ``.drv`` file becomes an
    input source of the planner, so the planner can name it, while the *output*
    of that derivation stays unbuilt.

``builtins.unsafeDiscardStringContext``
    Turns an ``outPath`` into a plain string. The planner learns where the
    output will land without depending on it.

The pair is what keeps a plan lazy. A candidate that the planner rejects is
never built, and never downloaded.

The Nix side of a menu looks like this:

.. code-block:: nix

    candidates = map (drv: {
      name = drv.name;
      drv = builtins.unsafeDiscardOutputDependency drv.drvPath;
      outputs = { out = builtins.unsafeDiscardStringContext drv.outPath; };
      meta = { ... };
    }) everyCandidate;

and the planner reads it with :func:`Menu.from_json`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from ._derivation import Derivation


def _no_outputs() -> dict[str, str]:
    return {}


def _no_meta() -> dict[str, object]:
    return {}


@dataclass(frozen=True, slots=True)
class Candidate:
    """One derivation that Nix instantiated and did not build."""

    name: str
    drv: str
    outputs: Mapping[str, str] = field(default_factory=_no_outputs)
    meta: Mapping[str, object] = field(default_factory=_no_meta)

    def output(self, name: str = "out") -> str:
        """The store path of output ``name``.

        Raises :class:`KeyError` when the menu carries no path for it, which
        is what a floating content-addressed candidate looks like: such a path
        does not exist until the build finishes, so no planner can name it.
        """
        try:
            return self.outputs[name]
        except KeyError:
            raise KeyError(
                f"{self.name}: the menu has no path for output {name!r}; "
                "a floating content-addressed output has none to give"
            ) from None


@dataclass(frozen=True, slots=True)
class Menu:
    """Every candidate that the Nix expression offered."""

    candidates: Sequence[Candidate]

    @classmethod
    def from_json(cls, text: str) -> Menu:
        """Read a menu from the JSON that the Nix expression wrote."""
        raw: object = json.loads(text)
        if not isinstance(raw, list):
            raise TypeError(f"a menu is a JSON list of candidates, got {type(raw).__name__}")
        entries = cast("list[object]", raw)
        return cls([_candidate(entry, index) for index, entry in enumerate(entries)])

    def __iter__(self) -> Iterator[Candidate]:
        return iter(self.candidates)

    def __len__(self) -> int:
        return len(self.candidates)

    def by_name(self, name: str) -> Candidate:
        """The one candidate called ``name``."""
        matches = [candidate for candidate in self.candidates if candidate.name == name]
        if len(matches) != 1:
            raise KeyError(f"expected one candidate named {name!r}, found {len(matches)}")
        return matches[0]

    def select(self, chosen: Sequence[Candidate]) -> dict[str, list[str]]:
        """The ``input_drvs`` mapping for ``chosen``.

        Pass the result to :class:`ddrn.Derivation`. Nix then builds exactly
        these candidates, and leaves the rest of the menu alone.
        """
        selected: dict[str, list[str]] = {}
        for candidate in chosen:
            selected.setdefault(candidate.drv, [])
            for output in candidate.outputs or {"out": ""}:
                if output not in selected[candidate.drv]:
                    selected[candidate.drv].append(output)
        return selected


def _candidate(entry: object, index: int) -> Candidate:
    if not isinstance(entry, dict):
        raise TypeError(f"candidate {index} is a {type(entry).__name__}, not an object")
    fields = cast("dict[str, object]", entry)

    name = fields.get("name")
    drv = fields.get("drv")
    if not isinstance(name, str) or not isinstance(drv, str):
        raise TypeError(f"candidate {index} needs a string 'name' and a string 'drv'")
    if not drv.endswith(".drv"):
        raise ValueError(f"candidate {index} ({name}): 'drv' is {drv!r}, which is not a .drv path")

    return Candidate(
        name=name,
        drv=drv,
        outputs=_string_map(fields.get("outputs"), f"candidate {index} ({name}): 'outputs'"),
        meta=_object_map(fields.get("meta"), f"candidate {index} ({name}): 'meta'"),
    )


def _object_map(value: object, what: str) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError(f"{what} is an object, not a {type(value).__name__}")
    entries = cast("dict[object, object]", value)
    return {key: item for key, item in entries.items() if isinstance(key, str)}


def _string_map(value: object, what: str) -> dict[str, str]:
    mapping = _object_map(value, what)
    for key, item in mapping.items():
        if not isinstance(item, str):
            raise TypeError(f"{what}[{key!r}] is a store path, not a {type(item).__name__}")
    return cast("dict[str, str]", mapping)


def check_inputs_available(derivation: Derivation, menu: Menu, extra: Sequence[str], store_dir: str) -> None:
    """Fail when the plan names a store path that the planner cannot account for.

    Nix reports such a path as an unaccounted reference of the text-hashed
    output, or as an invalid path at build time. Both messages arrive after
    the planner exits, and neither one says which line of the plan is wrong.
    This check runs while the planner can still say so.
    """
    known = {candidate.drv for candidate in menu}
    for candidate in menu:
        known.update(candidate.outputs.values())
    known.update(extra)

    unknown = sorted(derivation.referenced_paths(store_dir) - known)
    if unknown:
        listed = "\n  ".join(unknown)
        raise ValueError(
            f"{derivation.name}: the plan names store paths that are not on the menu "
            f"and are not inputs of the planner:\n  {listed}"
        )
