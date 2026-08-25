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

**That reason covers the bulk pass, and it does not cover one option at
render time.** ``fetch_option_values`` returns a *lazy* attrset of the same
options, keyed by the same name, and forces nothing. ``pynix._option_values``
selects one key and forces that, so an "attribute X missing" failure arrives
in Python as an ordinary exception that a ``try`` catches -- which is the
thing ``tryEval`` cannot do inside Nix. One option that cannot answer then
costs its own detail pane a line, and costs no other option anything.
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

#: The walk itself, shared by the two collectors below.
#:
#: It defines `walk`, which takes the function that describes one option and
#: returns one entry for each option under the tree. Both collectors are the
#: same walk with a different `entry`, and a copy of the walk in each one would
#: let the two disagree about which options exist.
#:
#: `lib` comes from the scope this is interpolated into. Both collectors are
#: functions of `lib` and `options`, so `lib` is in scope where they use this.
_WALK = f"""
    walkWith = entry: depth: opts:
      builtins.concatMap
        (opt:
          let
            v = opt.visible or true;
            subVisible = if builtins.isBool v then v else v == "transparent";
            sub = (opt.type or {{ }}).getSubOptions or (_: {{ }}) opt.loc;
          in
            [ (entry opt) ]
            ++ (if depth > 0 && subVisible && sub != {{ }} then walkWith entry (depth - 1) sub else [ ])
        )
        (lib.collect lib.isOption opts);
    walk = entry: walkWith entry {_SUB_OPTION_DEPTH};
"""

_COLLECT_OPTION_METADATA = f"""
lib: options:
  let
{_WALK}
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
  in
    walk entry options
"""

#: The second collector. It answers "what is the default of this one option",
#: and it forces no default until a caller asks for one.
#:
#: **The result is one attrset, keyed by the name the metadata walk writes.**
#: Nix builds an attrset lazily, so `builtins.listToAttrs` forces the walk and
#: every `lib.showOption` and forces no `default` and no `example` at all. A
#: caller selects one name, forces that, and pays for that option alone. An
#: option that cannot answer raises where the caller can catch it, which is
#: the whole difference from the bulk pass: the module docstring above says
#: why one list forced in one JSON pass makes one bad default the failure of
#: all 24 941.
#:
#: `defaultText` comes before `default`, because a module declares one exactly
#: for a default that cannot print. `lib.options.renderOptionValue` is the
#: same two branches, and this states them rather than depending on a name
#: under `lib.options`.
_COLLECT_OPTION_VALUES = f"""
lib: options:
  let
{_WALK}
    render = value:
      if lib.isAttrs value && value ? _type && value ? text
      then {{ type = value._type; text = value.text; }}
      else {{ type = "literalExpression"; text = lib.generators.toPretty {{ multiline = true; }} value; }};
    entry = opt: {{
      name = lib.showOption opt.loc;
      value = {{
        default =
          if opt ? defaultText then render opt.defaultText
          else if opt ? default then render opt.default
          else null;
        example = if opt ? example then render opt.example else null;
      }};
    }};
  in
    builtins.listToAttrs (walk entry options)
"""


#: Render one value of a configuration the way a `default` is rendered.
#:
#: **The same two branches as `_COLLECT_OPTION_VALUES`.** A value that a
#: module wrote through `lib.literalMD` or `lib.literalExpression` carries its
#: own text and says so, and every other value goes through
#: `lib.generators.toPretty`. A reader who compares the `default` line with
#: the `value` line under it must not be reading two different renderers.
#:
#: It takes `lib` and gives back a function of one value, so the caller walks
#: the attribute path from Python and never has to write a Nix path
#: expression. A quoted segment then needs no escaping at all.
_RENDER_ONE_VALUE = """
lib: value:
  if lib.isAttrs value && value ? _type && value ? text
  then { type = value._type; text = value.text; }
  else { type = "literalExpression"; text = lib.generators.toPretty { multiline = true; } value; }
"""


async def fetch_value_renderer(session: AsyncEvalSession, lib_value: AsyncValue) -> AsyncValue:
    """A function of one value, giving the rendered text of it.

    The caller selects the value it wants by walking attributes, and applies
    this to what it reaches. Nothing is forced until then.
    """
    renderer = await session.string(_RENDER_ONE_VALUE)
    return await renderer.call(lib_value)


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


async def fetch_option_values(
    session: AsyncEvalSession,
    options_value: AsyncValue,
    lib_value: AsyncValue,
) -> AsyncValue:
    """Return the lazy attrset of ``default``/``example``, keyed by option name.

    The keys are what :func:`fetch_option_doc_list` writes in
    ``OptionRecord.name``, so a caller selects one record's values by that
    name. Nothing under a key is forced here. ``pynix._option_values`` forces
    one at a time, and catches what one bad default raises.
    """
    collector = await session.string(_COLLECT_OPTION_VALUES)
    return await collector.call(lib_value, options_value)
