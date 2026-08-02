# Examples

Each script below is run by `grpclib-transports/tests/test_examples.py`, so a
change to the library that breaks one of them fails the test suite.

## Common service implementations

```{literalinclude} ../../grpclib-transports/docs/examples/services.py
:language: python
```

## Unix domain socket

```{literalinclude} ../../grpclib-transports/docs/examples/unix_example.py
:language: python
```

## Stdio subprocess

```{literalinclude} ../../grpclib-transports/docs/examples/stdio_example.py
:language: python
```

## Multiprocessing pipe pair

```{literalinclude} ../../grpclib-transports/docs/examples/multiprocessing_example.py
:language: python
```

## SSH

```{literalinclude} ../../grpclib-transports/docs/examples/ssh_example.py
:language: python
```

## Bidirectional logical RPC

```{literalinclude} ../../grpclib-transports/docs/examples/bidi_example.py
:language: python
```
