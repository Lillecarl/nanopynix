"""Bulk NixOS option extraction, shared by ``pynix osearch`` and (potentially) the LSP.

Extracting one option's fields at a time (the pattern
``_lsp/_module_system.py``'s ``render_option_declaration`` uses for hover) is
a handful of RPC round trips per option -- fine for a single hover lookup,
but a real ``nixosConfigurations.<host>`` can expose 10,000-20,000 options,
so that approach would take tens of thousands of round trips to index in
bulk. ``fetch_option_doc_list`` instead runs the whole recursive walk *inside*
the running Nix evaluator via ``ValueProxy.call()`` (which passes the
already-evaluated options attrset by RPC handle, not by re-expressing it as
source text) -- two RPC round trips total, independent of how many options
exist.

This deliberately does **not** use nixpkgs' own ``lib.optionAttrSetToDocList``
(the function ``nixos-render-docs``/search.nixos.org use), because that
function also renders each option's ``default``/``example`` -- which means
*evaluating* them. Many real-world options (notably disko's) define their
default as an expression over `config`, e.g. deriving a mount order from
sibling filesystem config -- something that only resolves once a whole
system is realized, not when generating a bare index of names/types/
descriptions. A single such option throws and aborts the *entire* bulk fetch
(it's one Nix list, forced in one JSON pass).

A ``builtins.tryEval``/``builtins.deepSeq`` guard around just the
``default``/``example`` fields was tried and does **not** work: per Nix's
own documentation for `tryEval` (`nix/src/libexpr/primops.cc`), it "only
prevents errors created by `throw` or `assert`" -- an "attribute X missing"
failure (exactly what an unevaluable `config`-dependent default throws) is a
builtin-generated error, which `tryEval` explicitly does not catch, even
combined with `deepSeq` (verified empirically: `builtins.tryEval ({}.b)`
fails uncaught). So this walk never forces `default`/`example` at all --
only `name`/`type`/`description`/`declarations`/`readOnly`, which are always
safe, static data on the option declaration itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

    from nanopynix.rpc.client import EvalSession, ValueProxy

_COLLECT_OPTION_METADATA = """
lib: options:
  map
    (opt: {
      name = lib.showOption opt.loc;
      description = opt.description or null;
      declarations = builtins.filter (x: x != lib.unknownModule) opt.declarations;
      internal = opt.internal or false;
      visible =
        let v = opt.visible or true;
        in if builtins.isBool v then v else v == "shallow";
      readOnly = opt.readOnly or false;
      type = opt.type.description or "unspecified";
    })
    (lib.collect lib.isOption options)
"""


@dataclass(frozen=True)
class OptionRecord:
    """One NixOS option's identifying metadata (deliberately no ``default``/``example``)."""

    name: str
    type: str
    description: str | None
    declarations: list[str]
    read_only: bool


async def fetch_option_doc_list(
    session: EvalSession,
    options_value: ValueProxy,
    lib_value: ValueProxy,
) -> list[OptionRecord]:
    """Bulk-extract every visible, non-internal option under *options_value*.

    *lib_value* must be a nixpkgs ``lib`` attrset (the one whose
    ``lib.collect``/``lib.isOption``/``lib.showOption`` should walk
    *options_value* -- ordinarily the same nixpkgs the options tree itself
    came from, though the walk is generic enough that any reasonably current
    ``lib`` works).
    """
    collector = await session.string(_COLLECT_OPTION_METADATA)
    doc_list_value = await collector.call(lib_value, options_value)
    raw = await doc_list_value.force_json()
    if not isinstance(raw, list):
        raise TypeError(f"option metadata walk must return a list, got {type(raw).__name__}")
    records: list[OptionRecord] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise TypeError(f"each option metadata entry must be an object, got {type(entry).__name__}")
        if entry.get("visible") and not entry.get("internal"):
            records.append(_parse_record(entry))
    return records


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    items = cast("list[object]", value)
    return [str(item) for item in items]


def _parse_record(entry: Mapping[str, object]) -> OptionRecord:
    description = entry.get("description")
    return OptionRecord(
        name=str(entry["name"]),
        type=str(entry.get("type", "")),
        description=description if isinstance(description, str) else None,
        declarations=_string_list(entry.get("declarations")),
        read_only=bool(entry.get("readOnly", False)),
    )
