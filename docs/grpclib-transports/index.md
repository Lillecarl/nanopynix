# grpclib-transports

gRPC over custom asyncio transports — stdio subprocess pipes, SSH sessions,
Unix-domain sockets, and multiprocessing pipe pairs.

This is a subproject of this repository (`grpclib-transports/`), not a
third-party dependency. nanopynix is its only consumer: the rpc engine runs
each Nix evaluator in a worker process and speaks gRPC to that process over
the stdio and multiprocessing transports below. The library is meant to be
changed for that consumer.

```{toctree}
:maxdepth: 2
:caption: Contents

examples
api
```

## Where to start

No quick-start snippet on this page, deliberately. Every transport already has
a complete, runnable script on the [Examples](examples.md) page, and
`grpclib-transports/tests/test_examples.py` runs each one — so a change that
breaks an example fails the suite, which is not true of a snippet written out
here. Read the script for the transport you need:

| Transport | Example | What it shows |
| --- | --- | --- |
| Unix-domain socket | `unix_example.py` | `Server.start_unix` and `connect_unix`, both in one process |
| Stdio subprocess | `stdio_example.py` | `stdio_worker` spawning a child that serves over its own stdin/stdout |
| Multiprocessing pipe pair | `multiprocessing_example.py` | a forkserver worker reached over dup'd OS pipes — the transport nanopynix's worker pool uses |
| SSH | `ssh_example.py` | `serve_ssh` and `connect_ssh` over an asyncssh session |
| Bidirectional logical RPC | `bidi_example.py` | `LogicalRpcPeer`, which multiplexes calls in both directions over one channel |

All five share the service implementations in `services.py`, and they talk to
`greeter-proto/`, the generated fixture service this repository builds for
exactly this purpose.

The [API reference](api.md) documents each transport, channel and server type
directly from its docstrings.
