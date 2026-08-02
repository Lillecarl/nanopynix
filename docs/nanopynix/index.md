# nanopynix

Eval Nix from Python — a fast, async-first Python binding for the Nix evaluator.

```{toctree}
:maxdepth: 2
:caption: Contents

architecture
architecture-principles
quality-gates
decisions
examples
api/index
```

## Quick start

<!-- example: hello_example.py#hello -->
```python
import asyncio

import nanopynix


async def main() -> None:
    async with (
        nanopynix.rpc.Session() as session,
        session.store() as store,
        session.eval(store) as evaluator,
    ):
        hello = await evaluator.string('"hello, world"')
        print(await hello.to_python())


if __name__ == "__main__":
    asyncio.run(main())
```

The `if __name__` guard is required rather than conventional. The rpc engine
starts its worker with the multiprocessing forkserver, which refuses to start
a child while the main module is still importing.
