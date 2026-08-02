# nanopynix

[![Documentation](https://img.shields.io/badge/docs-lillecarl.github.io-blue)](https://lillecarl.github.io/nanopynix/)
[![codecov](https://codecov.io/gh/Lillecarl/nanopynix/graph/badge.svg)](https://codecov.io/gh/Lillecarl/nanopynix)

nanobind-based Python bindings for Nix.

The high-level API runs Nix inside an isolated worker subprocess and talks to it
over gRPC:

<!-- example: quickstart_example.py#quickstart -->
```python
async with (
    nanopynix.rpc.Session(settings=NixSettings(max_jobs=4)) as session,
    session.store() as store,
    session.eval(store) as evaluator,
):
    store_dir = await store.store_dir()
    root = await evaluator.string('{ name = "hello"; }')
    name = await root.attr("name").as_string()

print(store_dir, name)
```

The whole program, with its imports, is
[`docs/examples/quickstart_example.py`](docs/examples/quickstart_example.py),
which the test suite runs.

Open multiple `Session` instances to run differently configured Nix instances in
parallel.

[Documentation](https://lillecarl.github.io/nanopynix/)

---

*This project is made possible by*

[![Dynamist](.assets/dynamist-logo.png)](https://dynamist.se/)
