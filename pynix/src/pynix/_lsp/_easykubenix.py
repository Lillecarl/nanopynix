"""easykubenix-specific inference for pynix's language server.

`easykubenix <https://github.com/Lillecarl/easykubenix>`_ is a NixOS-module-
style system for generating Kubernetes manifests. A file opts in with one
directive:

    # pynix-lsp: easykubenixEntry = import ./default.nix { }

Its expression must evaluate to an attrset shaped ``{ moduleSystem,
openApiSchemaPath }``:

- ``moduleSystem`` -- the *raw* ``lib.evalModules`` result (``.config``/
  ``.options``/``._module`` all present), exactly what ``_module_system.py``
  expects from a ``moduleEntry``. Unlike terranix, easykubenix's own
  ``default.nix`` already exposes this directly via ``passthru.eval`` -- no
  un-hiding workaround needed (see ``pynix/tests/test_lsp/easykubenix/
  default.nix``).
- ``openApiSchemaPath`` -- a string filesystem path to a Kubernetes OpenAPI
  v2 (``swagger.json``-shaped) JSON document, however the calling project
  chooses to produce it (a ``pkgs.fetchurl``-pinned upstream release, a
  live-cluster ``kubectl get --raw /openapi/v2`` dump cached to disk, ...).
  pynix only ever reads the path; producing it is the calling project's
  concern, same as ``tofu``/``module`` are for terranix.

(``pynix/tests/test_lsp/easykubenix/default.nix`` is the reference
implementation of this contract.)

``kubernetes.nix``'s object bodies (``kubernetes.objects.<namespace>.
<kind>.<name>``) are typed ``freeformType = ekn.lib.kubeValueType`` -- a
generic recursive JSON-ish type, so Nix itself has zero structural knowledge
of what e.g. a ``Deployment``'s ``spec`` contains. That knowledge only
exists in the Kubernetes OpenAPI schema, external to Nix -- exactly
analogous to how Terraform provider attribute schemas only exist in ``tofu
providers schema -json``, not in any Nix value (see ``_terranix.py``'s own
docstring for the parallel). This module bridges the two: ``derive_roots``
binds ``moduleSystem`` as a ``moduleEntry`` root (reusing
``_module_system.derive_module_roots``/``ModuleSystemDialect`` as-is, same
ordering reason as ``TerranixDialect`` -- see that class's own docstring),
while ``hover``/``complete`` here resolve schema-only knowledge (a field's
description/type) that no Nix value carries. Diagnostics are deliberately
not implemented yet -- see the project's own planning notes for why.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lsprotocol import types

from nanopynix._typechecking import BEARTYPING
from nanopynix.exceptions import NixError
from pynix._jsonschema import list_properties, render, walk
from pynix._lsp._dialect import Dialect
from pynix._lsp._easykubenix_schema import find_definition_key, load_schema, split_api_version
from pynix._lsp._module_system import CONFIG_NAME, MODULE_ENTRY_NAME, derive_module_roots
from pynix._lsp._syntax import completion_target_at, enclosing_binding_path_at, identifier_path_at

if TYPE_CHECKING or BEARTYPING:
    import nanopynix
    from pynix._lsp._context import FileContext

EASYKUBENIX_ENTRY_NAME = "easykubenixEntry"
_OBJECT_ROOT_NAMES = ("objects", "resources")
"""Both the real ``kubernetes.objects`` option and its ``kubernetes.resources`` alias (``mkAliasOptionModule``)."""

# Segment-count thresholds for `kubernetes.<objects|resources>.<ns>.<kind>.<name>.<field>...`.
_PATH_DEPTH_OBJECTS = 2  # [kubernetes, objects]
_PATH_DEPTH_NAMESPACE = 3  # [kubernetes, objects, ns]
_PATH_DEPTH_KIND = 4  # [kubernetes, objects, ns, kind]
_PATH_DEPTH_NAME = 5  # [kubernetes, objects, ns, kind, name]
_PATH_DEPTH_FIELD = 6  # [kubernetes, objects, ns, kind, name, field...]


def _object_path_at(source: str, byte_offset: int) -> list[str] | None:
    """Return the full attrpath at *byte_offset* if it's Kind-anchored (``kubernetes.objects.<ns>.<kind>...``).

    ``identifier_path_at`` alone only resolves the *innermost* binding's own
    flat attrpath -- for a field written in nested style (``Deployment.demo
    = { spec = { replicas = 1; }; };``, the natural NixOS-module way to
    write a Kubernetes object body), that alone loses the outer
    ``kubernetes.objects.<ns>.<kind>.<name>`` prefix entirely.
    ``enclosing_binding_path_at`` recovers it by climbing every wrapping
    attrset-valued binding, and is prepended unconditionally -- it's ``[]``
    for the fully-flat style, so this is a no-op there.
    """
    local = identifier_path_at(source, byte_offset)
    if local is None:
        return None
    path = enclosing_binding_path_at(source, byte_offset) + local
    if len(path) < _PATH_DEPTH_KIND or path[0] != "kubernetes" or path[1] not in _OBJECT_ROOT_NAMES:
        return None
    return path


class EasykubenixDialect(Dialect):
    """easykubenix support, as a peer ``Dialect`` (see ``_dialect.py``)."""

    async def derive_roots(self, context: FileContext) -> None:
        """Bind ``moduleSystem`` as a ``moduleEntry`` root and delegate to ``derive_module_roots``.

        Same ordering reason as ``TerranixDialect.derive_roots``: ``DIALECTS``
        runs ``ModuleSystemDialect`` before ``EasykubenixDialect`` (see
        ``_dialects.py``), so its own pass already ran and found no
        ``moduleEntry`` bound yet by the time this method sets one.
        """
        entry = context.roots.get(EASYKUBENIX_ENTRY_NAME)
        if entry is None:
            return
        context.roots.setdefault(MODULE_ENTRY_NAME, entry.attr("moduleSystem"))
        derive_module_roots(context)

    async def _attr_names(self, value: nanopynix.AsyncValue | None) -> list[str] | None:
        if value is None:
            return None
        try:
            return await value.attr_names()
        except NixError:
            return None

    async def _kind_names(self, context: FileContext) -> list[str] | None:
        """Every known Kind name, from ``config.kubernetes.apiMappings``'s own keys.

        Already a real, merged Nix value (bundled ``apiMappingFile`` plus any
        project-declared extras) -- a strictly richer source than the
        OpenAPI schema alone, since a CRD Kind can have an ``apiMappings``
        entry with no corresponding upstream schema definition at all.
        """
        config_root = context.roots.get(CONFIG_NAME)
        if config_root is None:
            return None
        return await self._attr_names(config_root.attr("kubernetes").attr("apiMappings"))

    async def _namespace_names(self, context: FileContext) -> list[str] | None:
        """Every declared namespace name, from existing ``Namespace`` objects in the ``none`` bucket.

        ``kubernetes.nix``'s own convention: a cluster-scoped ``Namespace``
        object itself lives under the fixed ``none`` bucket (``namespace !=
        "none"`` is what triggers auto-injecting ``metadata.namespace``), so
        ``kubernetes.objects.none.Namespace``'s own keys are exactly the set
        of namespace names this project has actually declared and is
        therefore useful to suggest -- as opposed to guessing at arbitrary
        strings, or listing every namespace bucket key already used
        elsewhere (which would suggest typos as confidently as real ones).
        """
        config_root = context.roots.get(CONFIG_NAME)
        if config_root is None:
            return None
        namespace_kind = config_root.attr("kubernetes").attr("objects").attr("none").attr("Namespace")
        return await self._attr_names(namespace_kind)

    async def _definition_for(self, context: FileContext, kind: str) -> tuple[dict[str, Any], str] | None:
        """Resolve *kind*'s ``(group, version, kind)`` schema definition, via ``config.kubernetes.apiMappings``."""
        entry = context.roots.get(EASYKUBENIX_ENTRY_NAME)
        config_root = context.roots.get(CONFIG_NAME)
        if entry is None or config_root is None:
            return None
        try:
            api_version = await config_root.attr("kubernetes").attr("apiMappings").attr(kind).to_python()
            schema_path = await entry.attr("openApiSchemaPath").to_python()
        except NixError:
            return None
        if not isinstance(api_version, str) or not isinstance(schema_path, str):
            return None
        group, version = split_api_version(api_version)
        schema = await load_schema(schema_path)
        key = find_definition_key(schema, group, version, kind)
        if key is None:
            return None
        return schema, key

    async def hover(
        self,
        context: FileContext,
        source: str,
        byte_offset: int,
        dialects: list[Dialect],
    ) -> str | None:
        del dialects
        path = _object_path_at(source, byte_offset)
        if path is None:
            return None
        definition = await self._definition_for(context, path[3])
        if definition is None:
            return None
        schema, key = definition
        ref = {"$ref": f"#/definitions/{key}"}
        if len(path) < _PATH_DEPTH_FIELD:
            # Cursor is on the Kind or object-instance name itself (``path``
            # has only ``[kubernetes, objects, ns, kind]`` or ``[..., name]``
            # segments, no field beyond that) -- there's no specific field to
            # describe, so this renders the definition as a whole.
            return render(ref, root=schema)
        fragment = walk(ref, tuple(path[5:]), root=schema)
        if fragment is None:
            return None
        return render(fragment, root=schema)

    async def complete(  # noqa: PLR0911 -- tracked complexity/arg-count debt, see TODO.md
        self,
        context: FileContext,
        source: str,
        byte_offset: int,
        dialects: list[Dialect],
    ) -> list[types.CompletionItem] | None:
        del dialects
        target = completion_target_at(source, byte_offset)
        if target is None:
            return None
        local_prefix, partial = target
        # Same nested-style recovery as `_object_path_at` -- `completion_target_at`
        # alone only sees the dotted chain typed so far in the *local*
        # binding, losing the outer `kubernetes.objects.<ns>.<kind>.<name>`
        # prefix for a field completed inside a nested-style object body.
        prefix = enclosing_binding_path_at(source, byte_offset) + local_prefix
        if len(prefix) < _PATH_DEPTH_OBJECTS or prefix[0] != "kubernetes" or prefix[1] not in _OBJECT_ROOT_NAMES:
            return None
        if len(prefix) == _PATH_DEPTH_OBJECTS:
            # Completing the namespace segment itself (e.g.
            # `kubernetes.objects.def<cursor>`) -- suggest names already
            # declared as real `Namespace` objects, not the (much larger,
            # much less relevant) set of every namespace bucket key already
            # used for some other Kind.
            names = await self._namespace_names(context)
            if names is None:
                return None
            return [types.CompletionItem(label=name) for name in names if name.startswith(partial)]
        if len(prefix) == _PATH_DEPTH_NAMESPACE:
            # Completing the Kind segment (e.g.
            # `kubernetes.objects.default.Depl<cursor>`) -- every Kind name
            # this project knows an apiVersion for, real or CRD.
            names = await self._kind_names(context)
            if names is None:
                return None
            return [types.CompletionItem(label=name) for name in names if name.startswith(partial)]
        if len(prefix) < _PATH_DEPTH_NAME:
            # The object's own instance name (e.g. `...Deployment.dem<cursor>`)
            # -- an arbitrary new name being chosen, no sensible source to
            # suggest from.
            return None
        definition = await self._definition_for(context, prefix[3])
        if definition is None:
            return None
        schema, key = definition
        names = list_properties({"$ref": f"#/definitions/{key}"}, tuple(prefix[5:]), root=schema)
        if names is None:
            return None
        return [types.CompletionItem(label=name) for name in names if name.startswith(partial)]
