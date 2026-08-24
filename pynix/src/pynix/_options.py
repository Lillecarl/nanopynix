"""Bulk NixOS option extraction, shared by ``pynix search`` and (potentially) the LSP.

Extracting one option's fields at a time (the pattern
``_lsp/_module_system.py``'s ``render_option_declaration`` uses for hover) is
a handful of RPC round trips per option -- fine for a single hover lookup,
but a real ``nixosConfigurations.<host>`` can expose 10,000-20,000 options,
so that approach would take tens of thousands of round trips to index in
bulk. ``fetch_option_doc_list`` instead runs the whole recursive walk *inside*
the running Nix evaluator via ``AsyncValue.call()`` (which passes the
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

**The walk follows ``opt.type.getSubOptions``, so a sub-option is in the
index.** An ``attrsOf (submodule ...)`` is one option to ``lib.collect
lib.isOption``, and the options inside the submodule are not in the tree at
all: they are behind that function. Without the recursion, not one of 14 752
options of a real ``nixosConfigurations`` held a ``<name>`` placeholder, and
``systemd.services.<name>.serviceConfig`` could not be found. This is the same
mechanism ``optionAttrSetToDocList`` uses, and it forces no ``default``, so it
keeps the property below. Measured on that configuration: 14 752 options
become 24 941, and the walk takes the same time, because the submodules were
evaluated already.

The recursion needs no filter of its own. Every submodule declares
``_module.args``, ``_module.check``, ``_module.freeformType`` and
``_module.specialArgs``, and each one is ``internal`` below the top level, so
the filter that this module already applies removes all four.

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

from nanopynix._typechecking import BEARTYPING

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Mapping

    from nanopynix import AsyncEvalSession, AsyncValue

#: How deep the walk follows a sub-option, before it stops.
#:
#: `lib.optionAttrSetToDocList` has no bound, and carries a comment on how to
#: find the infinite recursion when a type builds one. This walk states a bound
#: instead. Measured against a real `nixosConfigurations` of 24 941 options:
#: depth 4 reaches 24 935 of them and depth 6 reaches every one, so 8 leaves
#: margin for a tree deeper than any that exists today.
_SUB_OPTION_DEPTH = 8

_COLLECT_OPTION_METADATA = f"""
lib: options:
  let
    entry = opt: {{
      name = lib.showOption opt.loc;
      description = opt.description or null;
      declarations = builtins.filter (x: x != lib.unknownModule) opt.declarations;
      internal = opt.internal or false;
      visible =
        let v = opt.visible or true;
        in if builtins.isBool v then v else v == "shallow";
      readOnly = opt.readOnly or false;
      type = opt.type.description or "unspecified";
    }};
    walk = depth: opts:
      builtins.concatMap
        (opt:
          let
            v = opt.visible or true;
            subVisible = if builtins.isBool v then v else v == "transparent";
            sub = (opt.type or {{ }}).getSubOptions or (_: {{ }}) opt.loc;
          in
            [ (entry opt) ]
            ++ (if depth > 0 && subVisible && sub != {{ }} then walk (depth - 1) sub else [ ])
        )
        (lib.collect lib.isOption opts);
  in
    walk {_SUB_OPTION_DEPTH} options
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
    session: AsyncEvalSession,
    options_value: AsyncValue,
    lib_value: AsyncValue,
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
    raw = await doc_list_value.to_python()
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
