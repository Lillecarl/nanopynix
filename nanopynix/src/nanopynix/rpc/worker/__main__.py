"""``python -m nanopynix.rpc.worker`` — serve one worker over stdin and stdout.

This file exists so that the module the worker runs is not the module that
holds the worker. ``python -m nanopynix.rpc.worker._worker`` warns and runs
that module twice, because importing the package already imports it through
``nanopynix`` -> ``rpc`` -> ``client/_pool.py``. See
:mod:`nanopynix.rpc._worker_argv`, which builds the argument vector.

``nanopynix-worker``, the console script, calls the same function.
"""

from __future__ import annotations

from nanopynix.rpc.worker._worker import main

if __name__ == "__main__":
    main()
