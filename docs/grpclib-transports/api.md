# API Reference

## Protocol & Tuning

```{eval-rst}
.. autoclass:: grpclib_transports.TransportTuning
   :members:

.. autodata:: grpclib_transports.DEFAULT_TUNING

.. autoclass:: grpclib_transports.PeerIdentity
   :members:

.. autofunction:: grpclib_transports.local_process_identity
.. autofunction:: grpclib_transports.peer_identity_from_transport
.. autofunction:: grpclib_transports.peer_identity_from_stream

.. autofunction:: grpclib_transports.make_h2_config
.. autofunction:: grpclib_transports.make_config
.. autofunction:: grpclib_transports.make_server_protocol
.. autofunction:: grpclib_transports.init_h2_transport
.. autofunction:: grpclib_transports.build_mapping
.. autofunction:: grpclib_transports.pump
.. autofunction:: grpclib_transports.serve_h2
.. autofunction:: grpclib_transports.pause_h2_protocol
.. autofunction:: grpclib_transports.resume_h2_protocol
.. autofunction:: grpclib_transports.signal_stop

.. autofunction:: grpclib_transports.install_h2_fast_receive_patch
```

## Transports

```{eval-rst}
.. autoclass:: grpclib_transports.BaseCustomTransport
   :members:
   :show-inheritance:

.. autoclass:: grpclib_transports.StdioTransport
   :members:
   :show-inheritance:

.. autoclass:: grpclib_transports.SshTransport
   :members:
   :show-inheritance:

.. autoclass:: grpclib_transports.PipeTransport
   :members:
   :show-inheritance:
```

## Channels

```{eval-rst}
.. autoclass:: grpclib_transports.StdioChannel
   :members:
   :show-inheritance:

.. autoclass:: grpclib_transports.SshChannel
   :members:
   :show-inheritance:

.. autoclass:: grpclib_transports.PipeChannel
   :members:
   :show-inheritance:
```

## Server

```{eval-rst}
.. autoclass:: grpclib_transports.Server
   :members:
   :show-inheritance:

.. autofunction:: grpclib_transports.serve_stdio
.. autofunction:: grpclib_transports.serve_ssh
```

## Client

```{eval-rst}
.. autofunction:: grpclib_transports.connect_ssh
.. autofunction:: grpclib_transports.connect_tcp
.. autofunction:: grpclib_transports.connect_unix
.. autofunction:: grpclib_transports.stdio_worker
```

## Bidirectional RPC

```{eval-rst}
.. autoclass:: grpclib_transports.LogicalRpcPeer
   :members:
   :show-inheritance:

.. autoclass:: grpclib_transports.LogicalFrame
   :members:

.. autoexception:: grpclib_transports.RemoteCallError
.. autoexception:: grpclib_transports.PeerClosedError
```

## Workers

```{eval-rst}
.. autoclass:: grpclib_transports.StdioPeerPool
   :members:
   :show-inheritance:

.. autoclass:: grpclib_transports.PeerRegistry
   :members:
   :show-inheritance:

.. autoclass:: grpclib_transports.RegisteredPeer
   :members:
   :show-inheritance:
```

## Transfer

```{eval-rst}
.. autofunction:: grpclib_transports.iter_chunks
.. autofunction:: grpclib_transports.iter_file_chunks
```

## Multiprocessing

```{eval-rst}
.. autoclass:: grpclib_transports.MultiprocessingPipePair
   :members:
   :show-inheritance:

.. autoclass:: grpclib_transports.MultiprocessingPipeEndpoint
   :members:
   :show-inheritance:

.. autofunction:: grpclib_transports.multiprocessing_pipe_pair
.. autofunction:: grpclib_transports.get_worker_context
```
