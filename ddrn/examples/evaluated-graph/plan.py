"""Register a graph with the evaluator, and submit the root of it.

This script is the builder of `ddrn/examples/evaluated-graph/default.nix`. It
runs inside the sandbox of a `builder-rpc-v0` build, and it needs the two
changes to Nix that `ddrn/UPSTREAM.md` gives.

**Compare it with `ddrn/examples/submitted-graph/plan.py`.** That script builds
each derivation as a value and renders ATerm, because the released allowlist
refuses `EnsurePath` and so denies the evaluator the store context that a
derivation takes its inputs from. This script writes a Nix expression instead,
and the evaluator does the rest:

- `builtins.storePath` names each tool, and reaches `EnsurePath`. The restricted
  builder answers by asserting that the path is an input of this build.
- Each `derivation` call writes a `.drv` through `AddToStore`, which the
  allowlist always permitted.
- Interpolating one derivation into the script of another gives the dependency
  and the output path together. The ATerm form has to compute both by hand.

**The submitted object is the root `.drv` itself, and the outer derivation is
named `planner`.** The released rule compares the name of a submitted object
with the name that the output must carry, so the outer derivation had to be
named `graph.drv`. The name relaxation of this lab replaces that comparison
with a check of the derivation: the object must parse, and its own contents
must give the path where it sits.

**The graph mixes the two kinds of child.** `leaf-a` floats, so its output path
comes from the build. `leaf-b` is input-addressed, so the evaluator computes
its output path from a hash of the derivation modulo its inputs. Both work
here, because one `EvalState` writes every derivation of the graph and
memoises each hash modulo as it goes. A second process would have to read a
`.drv` back out of the store, and the allowlist refuses that read.
"""

from __future__ import annotations

import os
import sys

from nanopynix_bindings import expr as nix_expr, store as nix_store

import nanopynix

# The name of the root of the graph. Nothing outside this file depends on it.
ROOT_NAME = "graph"

# The graph. `builtins.storePath` is the line that the released allowlist
# refuses, and it is the reason this expression cannot run on a released Nix.
EXPRESSION = """
let
  bash = builtins.storePath "@bash@";
  coreutils = builtins.storePath "@coreutils@";

  contentAddressed = {
    __contentAddressed = true;
    outputHashMode = "recursive";
    outputHashAlgo = "sha256";
  };

  base =
    { name, script }:
    {
      inherit name;
      system = "@system@";
      builder = "${bash}/bin/bash";
      args = [ "-c" script ];
      PATH = "${coreutils}/bin";
    };

  # A floating content-addressed derivation, and an input-addressed one.
  ca = args: derivation (base args // contentAddressed);
  ia = args: derivation (base args);

  leafA = ca {
    name = "leaf-a";
    script = "echo alpha > $out";
  };

  leafB = ia {
    name = "leaf-b";
    script = "echo beta > $out";
  };
in
ca {
  name = "@root@";
  script = ''
    mkdir -p "$out"
    cp ${leafA} "$out/a"
    cp ${leafB} "$out/b"
  '';
}
"""


def env(name: str) -> str:
    """Read one environment variable that `default.nix` must set."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is empty: default.nix must set it")
    return value


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
    print(f"==> restricted store: {store.get_uri()}")

    expression = (
        EXPRESSION.replace("@bash@", env("DDRN_BASH"))
        .replace("@coreutils@", env("DDRN_COREUTILS"))
        .replace("@system@", env("DDRN_SYSTEM"))
        .replace("@root@", ROOT_NAME)
    )

    state = nix_expr.EvalState(store)
    root = state.eval_string(expression)

    # `derived_path` gives the store path of the `.drv` that the evaluator
    # wrote. It is not an output path: this build cannot realise anything, so
    # no output of that derivation exists yet.
    root_drv = root.derived_path()
    print(f"==> the evaluator wrote {root_drv}")

    store.submit_output(root_drv, "out")
    print("==> submitted it as output 'out'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
