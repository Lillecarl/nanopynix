# nanopynix

nanobind-based Python bindings for Nix.

The high-level API runs Nix inside an isolated worker subprocess and talks to it
over gRPC:

```python
import nanopynix
from nanopynix_proto.nix.store import GetStoreDirRequest

async with nanopynix.Session(config={"max-jobs": "4"}) as session:
    async with session.store() as store:
        store_dir = (await store.get_store_dir(GetStoreDirRequest())).dir

    async with session.eval() as eval_:
        root = await eval_.string('{ name = "hello"; }')
        attrs = await root.force()
        name = await attrs["name"].force()
```

Open multiple `Session` instances to run differently configured Nix instances in
parallel.
