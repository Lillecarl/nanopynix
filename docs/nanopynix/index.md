# nanopynix

Eval Nix from Python — a fast, async-first Python binding for the Nix evaluator.

```{toctree}
:maxdepth: 2
:caption: Contents

architecture
examples
api/index
```

## Quick start

```python
import asyncio
import nanopynix

async def main():
    async with nanopynix.rpc.Session() as session, session.store() as store, session.eval(store) as eval:
        hello = await eval.string('"hello, world"')
        print(await hello.force_json())

asyncio.run(main())
```
