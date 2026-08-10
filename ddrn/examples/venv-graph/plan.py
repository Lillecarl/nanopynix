"""Resolve a Python environment, and register it as a graph of derivations.

This script is the builder of `ddrn/examples/venv-graph/default.nix`. It runs
inside the sandbox of a `builder-rpc-v0` build, and it needs the three changes
to Nix that `ddrn/UPSTREAM.md` gives.

**Compare it with `ddrn/examples/venv/plan.py`.** That planner makes the same
packaging decisions and then emits **one** derivation, because a planner that
writes to `$out` can emit exactly one. So it installs every wheel in a single
build step, and it cannot build a source distribution at all: the backend that
builds the source would have to exist before the plan runs.

This planner emits a graph:

- one node for each wheel, which installs it;
- one node for each source distribution, which runs its PEP 517 backend;
- one node for each local project, which runs the PEP 660 backend of that
  project and then redirects the install at a tree outside the store;
- one node for the environment, which composes the members.

**The source distribution is the case that motivates the whole lab.** `idna`
is in the lock file as an sdist only. Its `pyproject.toml` asks for
`flit_core`, the planner resolves that name against the same lock file, and
the wheel of `flit_core` becomes another node of the same graph. The build of
`idna` then reads the output of that node. No node of that pair can exist
without the other, and neither can exist before the plan runs.

Every packaging decision below is made by `packaging`, which is the reference
implementation of the packaging PEPs, and none of it is expressed in the Nix
language:

- PEP 508 environment markers and requirements, through `packaging.markers`
  and `packaging.requirements`;
- PEP 440 version specifiers, through `packaging.specifiers`;
- PEP 503 name normalisation, through `packaging.utils.canonicalize_name`;
- PEP 425 and PEP 600 compatibility tags, through `packaging.tags` and
  `packaging.utils.parse_wheel_filename`.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nanopynix_bindings import expr as nix_expr, store as nix_store
from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.tags import Tag
from packaging.utils import canonicalize_name, parse_wheel_filename

import nanopynix

if TYPE_CHECKING:
    from collections.abc import Iterable

#: The backend that PEP 517 names when a project declares no `build-backend`.
FALLBACK_BACKEND = "setuptools.build_meta:__legacy__"


@dataclass(frozen=True, slots=True)
class Artifact:
    """One row of the lock file, with the two store paths that Nix gave it.

    `drv` is the store path of the `fetchurl` derivation, and `out` is the
    store path of its output. `default.nix` discards the dependency between
    the two, so Nix instantiates every artefact and downloads none of them.
    `graph.nix` puts the dependency back for the ones this planner chooses.
    """

    package: str
    version: str
    filename: str
    kind: str
    drv: str
    out: str
    marker: str | None = None
    backend: str | None = None
    backend_requires: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, row: dict[str, Any]) -> Artifact:
        return cls(
            package=row["package"],
            version=row["version"],
            filename=row["filename"],
            kind=row["kind"],
            drv=row["drv"],
            out=row["out"],
            marker=row.get("marker"),
            backend=row.get("backend"),
            backend_requires=tuple(row.get("backend-requires") or ()),
        )

    @property
    def node_name(self) -> str:
        """The name of the node that builds this artefact."""
        return f"{self.package}-{self.version}"

    @property
    def is_platform_wheel(self) -> bool:
        """Whether this wheel carries a binary, which its tag already says.

        A wheel tagged `any` runs on every platform, so it holds no compiled
        code. Any other tag names one platform, and the node that installs it
        needs the loader and the libraries of this system.
        """
        if self.kind != "wheel":
            return False
        _, _, _, wheel_tags = parse_wheel_filename(self.filename)
        return not any(tag.platform == "any" for tag in wheel_tags)

    def as_plan(self) -> dict[str, str]:
        # `filename` is the name that the index gave this artefact.
        # `install-wheel.sh` needs it, because the store path of the fetch has
        # a hash in front of that name and `installer` reads the name.
        return {"drv": self.drv, "out": self.out, "filename": self.filename}


@dataclass(frozen=True, slots=True)
class Target:
    """What this environment runs on, in the terms that `packaging` uses.

    The planner runs on one machine and plans for another, so the target is a
    value and not a property of the interpreter that reads this file.
    """

    #: The PEP 425 tags this environment accepts.
    tags: frozenset[Tag]
    #: The PEP 508 marker environment.
    markers: dict[str, str]

    def accepts(self, marker: str | None) -> bool:
        """Whether a PEP 508 marker holds for this target."""
        if marker is None:
            return True
        return Marker(marker).evaluate(self.markers)


@dataclass(frozen=True, slots=True)
class Editable:
    """One local project, and the tree that its install must read.

    `source` is a copy of the project inside the store, which the backend
    reads. `root` is what the install redirects to, and it is either a literal
    path or a reference to a variable that the environment reads at run time.
    """

    name: str
    source: str
    root: str

    @property
    def pyproject(self) -> dict[str, Any]:
        """The `pyproject.toml` of the project, read from the store copy."""
        with Path(self.source, "pyproject.toml").open("rb") as handle:
            return tomllib.load(handle)

    def build_system(self) -> tuple[str, list[Requirement]]:
        """The PEP 517 backend of this project, and what the backend needs.

        This is the same declaration that a source distribution carries, and
        the planner resolves it the same way. PEP 517 gives the fallback for a
        project that declares no backend.
        """
        table = self.pyproject.get("build-system", {})
        backend = table.get("build-backend", FALLBACK_BACKEND)
        return backend, [Requirement(text) for text in table.get("requires", [])]


@dataclass
class Plan:
    """The graph that this planner decided, in the shape `graph.nix` takes."""

    # A subscripted alias is callable and gives an empty container, and it also
    # tells the type checker what that container holds. A bare `list` here
    # leaves the element type unknown.
    nodes: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    members: list[str] = field(default_factory=list[str])
    installed: list[str] = field(default_factory=list[str])
    #: The name of the node that every install step reads.
    installer: str = ""
    _seen: set[str] = field(default_factory=set[str])

    def node(self, artifact: Artifact, kind: str, *, backend_path: list[str] | None = None) -> str:
        """Add one node, and return its name. A repeated artefact adds one node.

        `kind` is what the node *does*, and it is not always the kind of the
        artefact. `installer` is a wheel, and its node unpacks it rather than
        installs it, because nothing can install a wheel before that node
        exists.
        """
        name = artifact.node_name
        if name in self._seen:
            return name
        self._seen.add(name)
        entry: dict[str, Any] = {
            "name": name,
            "kind": kind,
            "artifact": artifact.as_plan(),
            # `graph.nix` gives a node the loader and the libraries of this
            # system only when the tag of the wheel says that the node needs
            # them. The tag is what `packaging` already parsed.
            "platform": artifact.is_platform_wheel,
        }
        if kind == "sdist":
            if artifact.backend is None:
                raise RuntimeError(f"{artifact.filename}: an sdist needs a 'backend' in the lock file")
            entry["backend"] = artifact.backend
            entry["backendPath"] = backend_path or []
        self.nodes.append(entry)
        return name

    def editable_node(self, editable: Editable, backend: str, backend_path: list[str]) -> str:
        """Add the node that builds one local project as an editable."""
        self.nodes.append(
            {
                "name": editable.name,
                "kind": "editable",
                "platform": False,
                "source": editable.source,
                "root": editable.root,
                "backend": backend,
                "backendPath": backend_path,
            }
        )
        return editable.name

    def member(self, name: str, label: str) -> None:
        """Mark a node as a member of the environment, and not only a build input.

        `label` is the line that the manifest of the environment carries.
        """
        self.members.append(name)
        self.installed.append(label)


def env(name: str) -> str:
    """Read one environment variable that `default.nix` must set."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is empty: default.nix must set it")
    return value


def target_tags(python_version: str, platform: str) -> set[Tag]:
    """The tags this environment accepts, as PEP 425 and PEP 600 define them.

    Built by hand rather than by `packaging.tags.sys_tags`, because the planner
    plans for a target that is not the machine it runs on.
    """
    major, minor = python_version.split(".")
    interpreter = f"cp{major}{minor}"

    platforms = [platform]
    if platform.startswith("linux_"):
        arch = platform.removeprefix("linux_")
        # A manylinux wheel names the oldest glibc it runs against, so every
        # version at or below the target is acceptable. The range is the one
        # that PEP 600 defines, and the floor of 2.17 covers manylinux2014.
        platforms += [f"manylinux_2_{glibc}_{arch}" for glibc in range(17, 40)]
        platforms += [f"manylinux2014_{arch}"]

    tags = {Tag(interpreter, interpreter, plat) for plat in platforms}
    tags |= {Tag(interpreter, "abi3", plat) for plat in platforms}
    tags |= {Tag(f"py{major}", "none", plat) for plat in [*platforms, "any"]}
    tags |= {Tag(f"py{major}{minor}", "none", "any")}
    return tags


def wheel_rank(artifact: Artifact, accepted: frozenset[Tag]) -> int | None:
    """How well a wheel fits, lower being better, or ``None`` for no fit.

    A platform-specific wheel outranks a pure one, which is what pip does:
    `charset_normalizer` ships a compiled wheel and an `any` fallback, and the
    compiled one is the one to install.
    """
    _, _, _, wheel_tags = parse_wheel_filename(artifact.filename)
    if not any(tag in accepted for tag in wheel_tags):
        return None
    return 1 if any(tag.platform == "any" for tag in wheel_tags) else 0


def choose(
    artifacts: Iterable[Artifact],
    requirement: Requirement,
    target: Target,
) -> Artifact | None:
    """The one artefact that satisfies `requirement` on this target.

    A wheel wins over a source distribution, and among wheels the best tag
    wins. A source distribution is the answer only when no wheel fits, which
    is what makes the build path of this example run.

    **The name of a requirement is not the name of a package until PEP 503
    normalises it.** A project asks for `flit_core`, and the index calls it
    `flit-core`. The version range is PEP 440, and `packaging` answers both.
    """
    package = canonicalize_name(requirement.name)
    best_wheel: tuple[int, Artifact] | None = None
    sdist: Artifact | None = None

    for artifact in artifacts:
        if canonicalize_name(artifact.package) != package:
            continue
        if artifact.version not in requirement.specifier:
            print(
                f"plan: {artifact.filename}: {artifact.version} is outside {requirement}",
                file=sys.stderr,
            )
            continue
        if not target.accepts(artifact.marker):
            print(f"plan: {artifact.filename}: marker excludes this target", file=sys.stderr)
            continue
        if artifact.kind == "sdist":
            sdist = artifact
            continue
        score = wheel_rank(artifact, target.tags)
        if score is None:
            continue
        if best_wheel is None or score < best_wheel[0]:
            best_wheel = (score, artifact)

    if best_wheel is not None:
        return best_wheel[1]
    return sdist


def resolve_backend(
    plan: Plan,
    artifacts: list[Artifact],
    requirements: Iterable[Requirement],
    target: Target,
    subject: str,
) -> list[str]:
    """Give each backend of `requirements` a node, and name those nodes.

    **This is the step that one derivation cannot express.** The backend comes
    from the same lock file as the thing it builds, so neither the backend node
    nor the node that reads it can exist before the plan runs.
    """
    names: list[str] = []
    for requirement in requirements:
        dependency = choose(artifacts, requirement, target)
        if dependency is None:
            raise RuntimeError(f"{subject}: no artefact of the lock file satisfies '{requirement}'")
        names.append(plan.node(dependency, dependency.kind))
        print(f"plan:   backend {dependency.filename} for {requirement}", file=sys.stderr)
    return names


def resolve(
    artifacts: list[Artifact],
    roots: list[str],
    installer: str,
    editables: list[Editable],
    target: Target,
) -> Plan:
    """Turn the lock file into the graph that builds the environment."""
    plan = Plan()

    # **The installer bootstraps, and it does so by unpacking.** Every other
    # node installs its wheel with `pypa/installer`, which writes `RECORD`, the
    # `.dist-info` and each console script. Nothing can install the installer
    # itself, so its node unzips the wheel and puts it on a path. This is the
    # same bootstrap that `pip` performs, and it is one node of the graph.
    bootstrap = choose(artifacts, Requirement(installer), target)
    if bootstrap is None:
        raise RuntimeError(f"the installer '{installer}' is not in the lock file")
    plan.installer = plan.node(bootstrap, "unpack")
    print(f"plan:   installer {bootstrap.filename} (unpacked to bootstrap)", file=sys.stderr)

    for package in roots:
        chosen = choose(artifacts, Requirement(package), target)
        if chosen is None:
            print(f"plan: {package}: no artefact fits this target", file=sys.stderr)
            continue

        backend_path: list[str] = []
        if chosen.kind == "sdist":
            backend_path = resolve_backend(
                plan,
                artifacts,
                [Requirement(text) for text in chosen.backend_requires],
                target,
                chosen.filename,
            )

        name = plan.node(chosen, chosen.kind, backend_path=backend_path)
        plan.member(name, f"{chosen.package}=={chosen.version}")
        binary = " (binary)" if chosen.is_platform_wheel else ""
        print(f"plan: {package}: {chosen.filename} ({chosen.kind}){binary}", file=sys.stderr)

    for editable in editables:
        # **A local project declares a build backend the same way an sdist
        # does.** The planner reads that declaration from the project itself,
        # and resolves it against the same lock file.
        backend, requirements = editable.build_system()
        backend_path = resolve_backend(plan, artifacts, requirements, target, f"{editable.name} (editable)")
        name = plan.editable_node(editable, backend, backend_path)
        plan.member(name, f"{editable.name} (editable, from {editable.root})")
        print(f"plan: {editable.name}: editable through {backend}, reading {editable.root}", file=sys.stderr)

    return plan


def nix_string(text: str) -> str:
    """`text` as a Nix string literal.

    A Nix string takes the escapes of C for the backslash and the quote, and
    it also takes `${` as the start of an interpolation. The JSON below holds
    store paths and file names, so `${` cannot appear, but an escape that
    depends on the input is not an escape.
    """
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("${", "\\${")
    return f'"{escaped}"'


def main() -> int:
    remote = os.environ.get("NIX_REMOTE", "")
    if not remote.startswith("unix://"):
        print(
            f"NIX_REMOTE is {remote!r}. This script runs only inside a "
            "builder-rpc-v0 build, which sets it to the restricted socket.",
            file=sys.stderr,
        )
        return 1

    # A `builder-rpc-v0` build gets no `$out`. The output arrives through
    # `submit_output` instead, so there is no path to write to.
    if "out" in os.environ:
        print(f"unexpected: out is set to {os.environ['out']!r}", file=sys.stderr)  # noqa: SIM112 -- Nix names this variable in lower case
        return 1

    nanopynix.init_libstore()
    nix_expr.init_libexpr()
    store = nix_store.open_store(remote)
    print(f"==> restricted store: {store.get_uri()}", file=sys.stderr)

    menu = json.loads(env("DDRN_MENU"))
    artifacts = [Artifact.from_json(row) for row in menu["artifacts"]]

    python_version = env("PYTHON_VERSION")
    platform = env("TARGET_PLATFORM")
    target = Target(
        tags=frozenset(target_tags(python_version, platform)),
        markers={
            "sys_platform": env("TARGET_SYS_PLATFORM"),
            "platform_system": "Linux",
            "os_name": "posix",
            "python_version": python_version,
            "platform_machine": platform.rsplit("_", 1)[-1],
            "implementation_name": "cpython",
        },
    )

    editables = [
        Editable(name=name, source=spec["source"], root=spec["root"])
        for name, spec in sorted(menu.get("editables", {}).items())
    ]

    plan = resolve(artifacts, menu["roots"], menu["installer"], editables, target)

    print(
        f"==> {len(plan.nodes)} nodes, {len(plan.members)} of them in the environment, from {len(artifacts)} artefacts",
        file=sys.stderr,
    )

    document = {
        "system": env("DDRN_SYSTEM"),
        "pythonVersion": python_version,
        "tools": menu["tools"],
        "scripts": menu["scripts"],
        "nodes": plan.nodes,
        "installer": plan.installer,
        "venv": {
            "name": env("DDRN_VENV_NAME"),
            "members": plan.members,
            "installed": plan.installed,
        },
    }

    # **The evaluator reads a fixed file, and the plan is its argument.** The
    # decisions above are data, and `graph.nix` is the code that turns them
    # into derivations. Nothing here writes a Nix expression by hand.
    expression = f"import {env('DDRN_GRAPH_NIX')} (builtins.fromJSON {nix_string(json.dumps(document))})"

    state = nix_expr.EvalState(store)
    root = state.eval_string(expression)

    # `derived_path` gives the store path of the `.drv` that the evaluator
    # wrote. It is not an output path: this build cannot realise anything.
    root_drv = root.derived_path()
    print(f"==> the evaluator wrote {root_drv}", file=sys.stderr)

    store.submit_output(root_drv, "out")
    print("==> submitted it as output 'out'", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
