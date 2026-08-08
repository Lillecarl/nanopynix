"""Choose one wheel of each package, and emit the derivation that installs them.

Every decision here is made by `packaging`, which is the reference
implementation of the packaging PEPs. None of it is reimplemented, and none of
it is expressed in the Nix language:

- PEP 508 environment markers, through `packaging.markers`.
- PEP 425 and PEP 600 compatibility tags, through `packaging.tags` and
  `packaging.utils.parse_wheel_filename`.

That is the whole argument for a planner. A Nix expression that made these
decisions would have to carry a copy of this logic, and would have to keep the
copy correct as the PEPs change.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

from packaging.markers import Marker
from packaging.tags import Tag
from packaging.utils import parse_wheel_filename

import ddrn

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ddrn import Candidate

#: The install script of the emitted derivation. It unpacks each chosen wheel
#: into one site-packages directory, which is the shape that `PYTHONPATH`
#: expects. A real implementation would also write `RECORD`, the entry-point
#: scripts and the `.dist-info` of the environment itself.
INSTALL_SCRIPT = """
set -eu
site="$out/lib/python$PYTHON_VERSION/site-packages"
mkdir -p "$site" "$out/bin"
for wheel in $WHEELS; do
  echo "installing $(basename "$wheel")" >&2
  unzip -q -o "$wheel" -d "$site"
done
cat > "$out/bin/python" <<EOF
#!$BASH_PATH/bin/bash
export PYTHONPATH="$site\\${PYTHONPATH:+:\\$PYTHONPATH}"
exec "$PYTHON/bin/python3" "\\$@"
EOF
chmod +x "$out/bin/python"
printf '%s\\n' $INSTALLED > "$out/manifest"
"""


def target_tags(python_version: str, platform: str) -> set[Tag]:
    """The tags this environment accepts, as PEP 425 and PEP 600 define them.

    Built by hand rather than by `packaging.tags.sys_tags`, because the
    planner plans for a target that is not the machine it runs on.
    """
    major, minor = python_version.split(".")
    interpreter = f"cp{major}{minor}"
    abi = f"cp{major}{minor}"

    platforms = [platform]
    if platform.startswith("linux_"):
        arch = platform.removeprefix("linux_")
        # A manylinux wheel names the oldest glibc it runs against, so every
        # version at or below the target is acceptable. The range is the one
        # that PEP 600 defines; the floor of 2.17 covers manylinux2014.
        platforms += [f"manylinux_2_{minor_glibc}_{arch}" for minor_glibc in range(17, 40)]
        platforms += [f"manylinux2014_{arch}", f"manylinux_2_17_{arch}"]

    tags = {Tag(interpreter, abi, plat) for plat in platforms}
    tags |= {Tag(interpreter, "abi3", plat) for plat in platforms}
    tags |= {Tag(f"py{major}", "none", plat) for plat in [*platforms, "any"]}
    tags |= {Tag(f"py{major}{minor}", "none", "any")}
    return tags


def marker_applies(marker: object, environment: dict[str, str]) -> bool:
    """Whether a PEP 508 marker holds for this target."""
    if marker is None:
        return True
    if not isinstance(marker, str):
        raise TypeError(f"a marker is a string or null, got {type(marker).__name__}")
    return Marker(marker).evaluate(environment)


def rank(candidate: Candidate, accepted: set[Tag]) -> int | None:
    """How well a wheel fits, lower being better, or ``None`` for no fit.

    A platform-specific wheel outranks a pure one, which is what pip does:
    `charset_normalizer` ships a compiled wheel and an `any` fallback, and the
    compiled one is the one to install.
    """
    filename = str(candidate.meta["filename"])
    _, _, _, wheel_tags = parse_wheel_filename(filename)
    if not any(tag in accepted for tag in wheel_tags):
        return None
    return 1 if any(tag.platform == "any" for tag in wheel_tags) else 0


def choose(menu: Iterable[Candidate], accepted: set[Tag], environment: dict[str, str]) -> list[Candidate]:
    """One wheel for each package that this target needs."""
    best: dict[str, tuple[int, Candidate]] = {}
    for candidate in menu:
        package = str(candidate.meta["package"])
        if not marker_applies(candidate.meta.get("marker"), environment):
            print(f"plan: {candidate.name}: marker excludes this target", file=sys.stderr)
            continue
        score = rank(candidate, accepted)
        if score is None:
            continue
        current = best.get(package)
        if current is None or score < current[0]:
            best[package] = (score, candidate)
    return [candidate for _, candidate in sorted(best.values(), key=lambda pair: pair[1].name)]


def main() -> None:
    env = ddrn.PlannerEnv.from_environ()
    python_version = os.environ["PYTHON_VERSION"]
    platform = os.environ["TARGET_PLATFORM"]

    environment = {
        "sys_platform": os.environ["TARGET_SYS_PLATFORM"],
        "platform_system": "Linux",
        "os_name": "posix",
        "python_version": python_version,
        "platform_machine": platform.rsplit("_", 1)[-1],
        "implementation_name": "cpython",
    }

    accepted = target_tags(python_version, platform)
    chosen = choose(env.menu, accepted, environment)

    print(f"plan: {len(chosen)} of {len(env.menu)} wheels selected", file=sys.stderr)
    for candidate in chosen:
        print(f"plan:   {candidate.name}", file=sys.stderr)

    bash = env.tool("bash")
    env.emit(
        ddrn.Derivation(
            name="demo-venv",
            system=env.system,
            builder=f"{bash}/bin/bash",
            args=["-c", INSTALL_SCRIPT],
            env={
                "PATH": f"{env.tool('coreutils')}/bin:{env.tool('unzip')}/bin",
                "PYTHON_VERSION": python_version,
                "BASH_PATH": bash,
                "PYTHON": env.tool("python"),
                "WHEELS": " ".join(candidate.output() for candidate in chosen),
                "INSTALLED": " ".join(
                    f"{candidate.meta['package']}=={candidate.meta['version']}" for candidate in chosen
                ),
            },
            input_srcs=[bash, env.tool("coreutils"), env.tool("unzip"), env.tool("python")],
            input_drvs=env.menu.select(chosen),
        )
    )


if __name__ == "__main__":
    main()
