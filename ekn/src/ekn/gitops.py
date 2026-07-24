from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

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
    """
    result: defaultdict[GitOpsTarget, list[Manifest]] = defaultdict(list)
    for name, entry in gitops_targets.items():
        if not isinstance(entry, dict):
            raise GitOpsTargetError(f"GitOps target {name!r} entry must be an attribute set")
        objects = _as_manifest_list(entry.get("objects"), name)
        target = GitOpsTarget(path=_required_string(entry.get("target"), "path"))
        result[target].extend(objects)
    return dict(result)


__all__ = ["GitOpsTarget", "GitOpsTargetError", "resolved_targets"]
