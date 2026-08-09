"""Register a graph of derivations from inside a `builder-rpc-v0` build.

This script is the builder of `ddrn/examples/submitted-graph/default.nix`. It
runs inside the sandbox of a content-addressing derivation that asks for the
`builder-rpc-v0` system feature. Nix gives that build a restricted daemon
socket at `$NIX_REMOTE` and no `$out`.

**The socket permits six worker operations and no more** (`daemon.cc:326`):
the four `Add*` operations, `AddTempRoot`, `IsValidPath` and `SubmitOutput`.
Everything this script does stays inside that set, and each step below says
which operation it uses.

That allowlist rules out two things a planner might otherwise reach for:

- **It cannot build.** `BuildPaths` is not permitted. So this script registers
  a graph and submits its root `.drv`; the consumer of the outer derivation
  realises that graph with `builtins.outputOf`.
- **It cannot use the evaluator.** A derivation takes its inputs from string
  context, and both primops that attach context, `builtins.storePath` and
  `builtins.appendContext`, call `EnsurePath`, which is operation 10 and is
  not permitted. So the derivations here are built as values and rendered by
  `Derivation.to_aterm`, which is Nix's own ATerm writer and needs no store at
  all.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any

from nanopynix_bindings import store as nix_store

import nanopynix

# The name of the root of the graph. The submitted store object is that root's
# `.drv`, and Nix checks the name of a submitted object against the name that
# the output must carry, which for `out` is the name of the outer derivation
# (`derivation-check.cc:109`). A `.drv` is named `<name>.drv`, so the outer
# derivation is named `graph.drv`. `default.nix` must agree.
ROOT_NAME = "graph"


@dataclass(frozen=True, slots=True)
class Tools:
    """What every derivation of the graph needs, read once from the sandbox.

    Each path is an input of the outer derivation, so each is present in the
    sandbox and is a legal `input_srcs` entry of the derivations below.
    """

    system: str
    bash: str
    coreutils: str

    @classmethod
    def from_environ(cls) -> Tools:
        return cls(system=env("DDRN_SYSTEM"), bash=env("DDRN_BASH"), coreutils=env("DDRN_COREUTILS"))


def env(name: str) -> str:
    """Read one environment variable that `default.nix` must set."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is empty: default.nix must set it")
    return value


def node(*, name: str, script: str, tools: Tools, input_drvs: dict[str, Any]) -> dict[str, Any]:
    """One derivation, in the shape that `Derivation.from_dict` takes.

    The output is `Deferred`, which means "input-addressed, path not filled in
    yet". `fill_in_output_paths` computes it. A planner could not compute such
    a path itself, because it comes from the hash of the derivation.
    """
    return {
        "name": name,
        "outputs": {"out": {"type": "Deferred"}},
        "input_srcs": [tools.bash, tools.coreutils],
        "input_drvs": input_drvs,
        "system": tools.system,
        "builder": f"{tools.bash}/bin/bash",
        "args": ["-c", script],
        # `out` is empty for the same reason the output is `Deferred`.
        # `fill_in_output_paths` fills both.
        "env": {"out": "", "name": name, "PATH": f"{tools.coreutils}/bin"},
        "structured_attrs": None,
    }


def register(store: Any, cfg: Any, value: dict[str, Any]) -> tuple[str, str]:
    """Complete one derivation and put it in the store.

    Returns its `.drv` path and the path of its `out` output.

    `fill_in_output_paths` is pure. `write_derivation` uses `AddTempRoot`,
    `IsValidPath` and `AddToStoreFromDump`, and all three are permitted. It
    also memoises the hash modulo of what it writes, which is what lets the
    *next* call fill in its own paths without reading this `.drv` back out of
    the store -- a read that the allowlist would refuse.
    """
    drv = nix_store.Derivation.from_dict(cfg, value)
    drv.fill_in_output_paths(store)
    drv_path = store.write_derivation(drv)
    # `fill_in_output_paths` turns each `Deferred` output into an
    # `InputAddressed` one, which carries a path. An output that still has no
    # path means the derivation was not the input-addressed shape that `node`
    # builds, so stop here rather than register a graph that cannot build.
    out = drv.to_dict(cfg)["outputs"]["out"]
    out_path = out.get("path")
    if out_path is None:
        raise RuntimeError(f"{drv.name}: the output 'out' has no path after fill_in_output_paths")
    print(f"==> {drv_path}")
    print(f"      out -> {out_path}")
    return drv_path, out_path


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
    store = nix_store.open_store(remote)
    cfg = nix_store.StoreDirConfig(store.get_store_dir())
    print(f"==> restricted store: {store.get_uri()}")

    tools = Tools.from_environ()

    # Two leaves, and a root that reads both. The root names each leaf twice:
    # in `input_drvs`, which is what makes Nix build them first, and in its
    # script, which needs the output path that `register` returned.
    leaf_a_drv, leaf_a_out = register(
        store, cfg, node(name="leaf-a", script='echo alpha > "$out"', input_drvs={}, tools=tools)
    )
    leaf_b_drv, leaf_b_out = register(
        store, cfg, node(name="leaf-b", script='echo beta > "$out"', input_drvs={}, tools=tools)
    )

    root_drv, _ = register(
        store,
        cfg,
        node(
            name=ROOT_NAME,
            script=(f'mkdir -p "$out"\ncp {leaf_a_out} "$out/a"\ncp {leaf_b_out} "$out/b"\n'),
            input_drvs={
                leaf_a_drv: {"outputs": ["out"], "dynamic_outputs": {}},
                leaf_b_drv: {"outputs": ["out"], "dynamic_outputs": {}},
            },
            tools=tools,
        ),
    )

    # The one operation that `builder-rpc-v0` adds. The submitted object is the
    # root `.drv` itself, and not an output of it, because this build cannot
    # realise anything.
    store.submit_output(root_drv, "out")
    print(f"==> submitted {root_drv} as output 'out'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
