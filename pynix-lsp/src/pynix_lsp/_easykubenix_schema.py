"""Loads and indexes a Kubernetes OpenAPI v2 (``swagger.json``-shaped) schema for easykubenix files.

Much simpler than ``_terranix_schema.py``'s job: there's no subprocess to
run (no ``tofu init``/schema-export equivalent) -- an easykubenix project's
own ``# pynix-lsp: easykubenixEntry = ...`` directive is responsible for
producing ``openApiSchemaPath`` (a plain filesystem path to the JSON
document itself, however the project chooses to get it there: a
``pkgs.fetchurl``-pinned upstream release, a live-cluster
``kubectl get --raw /openapi/v2`` dump cached to disk, ...) -- this module
only ever reads and indexes whatever's already at that path.

A real Kubernetes OpenAPI v2 document's ``definitions`` are already almost
directly consumable JSON Schema (confirmed against a real fetched schema):
internal ``$ref: "#/definitions/<key>"``s resolve correctly through
``pynix_lsp._jsonschema``'s new ``root``-aware ``walk``/``list_properties``/
``render`` once the whole document (not just one definition) is passed
through as ``root``. This module's only real job is finding *which*
``definitions`` key corresponds to a given Kind: every object-shaped
definition carries an ``x-kubernetes-group-version-kind`` list of
``{group, version, kind}`` triples -- indexing by that (built once per
loaded document, not hardcoded against the ``io.k8s.api.<group>.<version>.
<Kind>`` naming convention, which isn't perfectly uniform across every API
group) is what ``find_definition_key`` does.
"""

from __future__ import annotations

import json
from typing import Any, cast

import anyio

_MEM_CACHE: dict[str, dict[str, Any]] = {}
_GVK_INDEX_CACHE: dict[int, dict[tuple[str, str, str], str]] = {}
_CACHE_LOCK = anyio.Lock()


async def load_schema(schema_path: str) -> dict[str, Any]:
    """Return the parsed OpenAPI document at *schema_path*.

    Cached in-memory for the server's lifetime, keyed by *schema_path*
    itself -- a Nix store path is content-addressed (a changed schema
    necessarily has a different path), so no separate invalidation is
    needed, exactly like ``_terranix_schema.py``'s ``tofu_out``/``module_out``
    cache key.
    """
    cached = _MEM_CACHE.get(schema_path)
    if cached is not None:
        return cached
    async with _CACHE_LOCK:
        cached = _MEM_CACHE.get(schema_path)
        if cached is not None:
            return cached
        text = await anyio.Path(schema_path).read_text()
        schema = cast("dict[str, Any]", json.loads(text))
        _MEM_CACHE[schema_path] = schema
        return schema


def split_api_version(api_version: str) -> tuple[str, str]:
    """Split a Kind's ``apiVersion`` (e.g. easykubenix's ``kubernetes.apiMappings.<kind>``) into ``(group, version)``.

    Core-group kinds have no group prefix at all (``"v1"`` -> ``("", "v1")``,
    matching ``x-kubernetes-group-version-kind``'s own empty-string
    ``group`` for the core API group); everything else is ``"group/version"``
    (``"apps/v1"`` -> ``("apps", "v1")``).
    """
    if "/" not in api_version:
        return "", api_version
    group, version = api_version.split("/", 1)
    return group, version


def _gvk_index(schema: dict[str, Any]) -> dict[tuple[str, str, str], str]:
    """Build (and cache, keyed by the schema dict's identity) a ``(group, version, kind) -> definitions key`` index."""
    cached = _GVK_INDEX_CACHE.get(id(schema))
    if cached is not None:
        return cached
    index: dict[tuple[str, str, str], str] = {}
    definitions = schema.get("definitions")
    if isinstance(definitions, dict):
        for key, value in cast("dict[str, Any]", definitions).items():
            if not isinstance(value, dict):
                continue
            # isinstance narrows to the bare, generic-less `dict[Unknown,
            # Unknown]` (isinstance can't check generic parameters at
            # runtime) -- re-asserting the type here is what stops that
            # Unknown from propagating into the .get() calls below (same
            # idiom as pynix_lsp._jsonschema._as_str_keyed_dict).
            value = cast("dict[str, Any]", value)
            gvks = value.get("x-kubernetes-group-version-kind")
            if not isinstance(gvks, list):
                continue
            for gvk in cast("list[Any]", gvks):
                if not isinstance(gvk, dict):
                    continue
                gvk = cast("dict[str, Any]", gvk)
                group = gvk.get("group")
                version = gvk.get("version")
                kind = gvk.get("kind")
                if isinstance(group, str) and isinstance(version, str) and isinstance(kind, str):
                    index[(group, version, kind)] = key
    _GVK_INDEX_CACHE[id(schema)] = index
    return index


def find_definition_key(schema: dict[str, Any], group: str, version: str, kind: str) -> str | None:
    """Return the ``definitions`` key for *(group, version, kind)*, or None if not present in *schema*.

    None is a real, expected outcome: a Kind from a CustomResourceDefinition
    not installed on whichever cluster/schema-source produced *schema* (or
    any Kind at all when *schema* is a static, CRD-less upstream release
    dump) simply isn't indexable -- callers must treat this the same way
    ``_terranix.py``'s ``_block_for`` treats an unrecognized resource type:
    quietly offer nothing, not an error.
    """
    return _gvk_index(schema).get((group, version, kind))
