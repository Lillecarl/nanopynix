from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from ekn.apply import Manifest
    from nanopynix.models import JsonValue


class GitOpsTargetError(ValueError):
    """Raised when Nix-produced GitOps routing data is invalid."""


@dataclass(frozen=True)
class GitOpsTarget:
    path: str


def _required_string(route: JsonValue, field: str) -> str:
    if not isinstance(route, dict):
        raise GitOpsTargetError("GitOps target must be an attribute set")
    value = route.get(field)
    if not isinstance(value, str) or not value:
        raise GitOpsTargetError(f"GitOps target {field} must be a non-empty string")
    return value


def _as_manifest_list(objects: JsonValue, name: str) -> list[Manifest]:
    if not isinstance(objects, list):
        raise GitOpsTargetError(f"GitOps target {name!r} objects must be a list")
    manifests: list[Manifest] = []
    for obj in objects:
        if not isinstance(obj, dict):
            raise GitOpsTargetError(f"GitOps target {name!r} object must be an attribute set")
        manifests.append(obj)
    return manifests


def _as_path_list(paths: JsonValue, name: str) -> list[str]:
    if paths is None:
        return []
    if not isinstance(paths, list):
        raise GitOpsTargetError(f"GitOps target {name!r} rawFiles must be a list")
    result: list[str] = []
    for path in paths:
        if not isinstance(path, str):
            raise GitOpsTargetError(f"GitOps target {name!r} rawFiles entries must be strings")
        result.append(path)
    return result


def load_raw_manifest(path: str) -> Manifest:
    """Read a `kubernetes.rawFiles` entry from disk and parse it -- in
    Python, not Nix. `yaml.safe_load` parses JSON too (JSON is a YAML
    subset) and, unlike `builtins.toJSON`, never reorders keys: Python
    dicts are insertion-ordered, and the loader inserts each key in the
    order it appears in the source document. That's the entire point of
    `rawFiles` -- see its description in easykubenix's kubernetes.nix.
    """
    data: JsonValue = yaml.load(Path(path).read_text(), Loader=yaml.CSafeLoader)
    if not isinstance(data, dict):
        raise GitOpsTargetError(f"raw manifest {path!r} must be a JSON/YAML object")
    return data


def resolved_targets(gitops_targets: dict[str, JsonValue]) -> dict[GitOpsTarget, list[Manifest]]:
    """Turn `kubernetes.gitOpsTargets` (already joined by the Nix module) into
    `{GitOpsTarget: [manifest, ...]}`.

    The Nix side (`kubernetes.gitOpsTargets`) has already resolved each
    object's `ekn.gitOpsTarget` name against `gitOps.targets` and grouped
    objects by target name -- there is no index/lookup left to build here,
    just validation and a merge for the (unusual but valid) case of two
    named targets sharing the same path. The branch these all land on is
    instance-wide (`gitOps.deployBranch`/`gitOps.sourceBranch`), not part of
    a target -- targets are pure path-routing.

    Each target's `rawFiles` (paths only, per easykubenix) are read and
    parsed here and appended to the same manifest list `objects` populates
    -- from here on a raw-file-sourced manifest is just a dict like any
    other; `flatten_manifests` writes it via the same `yaml.dump(...,
    sort_keys=False)` call either way, which is what actually preserves
    its order (parsed-from-file dicts already have the right order;
    Nix-evaluated ones don't, but weren't order-sensitive to begin with).
    """
    result: defaultdict[GitOpsTarget, list[Manifest]] = defaultdict(list)
    for name, entry in gitops_targets.items():
        if not isinstance(entry, dict):
            raise GitOpsTargetError(f"GitOps target {name!r} entry must be an attribute set")
        objects = _as_manifest_list(entry.get("objects"), name)
        raw_paths = _as_path_list(entry.get("rawFiles"), name)
        target = GitOpsTarget(path=_required_string(entry.get("target"), "path"))
        result[target].extend(objects)
        result[target].extend(load_raw_manifest(p) for p in raw_paths)
    return dict(result)


__all__ = ["GitOpsTarget", "GitOpsTargetError", "load_raw_manifest", "resolved_targets"]
