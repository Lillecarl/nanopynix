"""Transitive closure over the Nix store SQLite graph — pure Python.

Replicates ``Store::computeFSClosure`` using only sqlite3.  Useful for
daemon implementations (pynixd) that own the Nix database directly.

Schema assumed (from ``src/libstore/schema.sql``)::

    ValidPaths(id INTEGER PK, path TEXT UNIQUE, hash, registrationTime,
               deriver, narSize, ultimate, sigs, ca)
    Refs(referrer FK→ValidPaths.id, reference FK→ValidPaths.id)
    DerivationOutputs(drv FK→ValidPaths.id, id TEXT, path TEXT)
"""

from __future__ import annotations

import sqlite3
from typing import Callable


def _q_forward_refs() -> str:
    return """
        SELECT v2.path FROM Refs
        JOIN ValidPaths v1 ON Refs.referrer = v1.id
        JOIN ValidPaths v2 ON Refs.reference = v2.id
        WHERE v1.path = ?
    """


def _q_backward_refs_excl_self() -> str:
    return """
        SELECT v1.path FROM Refs
        JOIN ValidPaths v1 ON Refs.referrer = v1.id
        WHERE reference = (SELECT id FROM ValidPaths WHERE path = ?)
          AND v1.path != ?
    """


def _q_deriv_outputs() -> str:
    return """
        SELECT path FROM DerivationOutputs
        WHERE drv = (SELECT id FROM ValidPaths WHERE path = ?)
    """


def _q_deriv_derivers() -> str:
    return """
        SELECT v.path FROM DerivationOutputs d
        JOIN ValidPaths v ON d.drv = v.id
        WHERE d.path = ?
    """


def _q_has_deriver() -> str:
    return """SELECT deriver FROM ValidPaths WHERE path = ?"""


def compute_fs_closure(
    db: sqlite3.Connection,
    start_paths: set[str],
    *,
    flip_direction: bool = False,
    include_outputs: bool = False,
    include_derivers: bool = False,
) -> set[str]:
    """Transitive closure over the Nix store SQLite graph.

    Args:
        db: An open sqlite3 connection to the Nix database.
        start_paths: Store-path basenames to start from
            (e.g. ``"0009k295vpq8lrrf9c5h3dp55mx6s0v2-utf8proc-2.11.0.drv"``).
        flip_direction: Walk referrers instead of references.
        include_outputs: Follow derivation → output edges.
        include_derivers: Follow path → deriver edges.
    """

    edges: list[Callable[[str], set[str]]] = []

    if flip_direction:
        edges.append(
            lambda path: {
                r
                for (r,) in db.execute(_q_backward_refs_excl_self(), (path, path))
            }
        )
        if include_outputs:
            edges.append(
                lambda path: {
                    r for (r,) in db.execute(_q_deriv_derivers(), (path,))
                }
            )
        if include_derivers:
            edges.append(
                lambda path: {
                    r
                    for (r,) in db.execute(_q_deriv_outputs(), (path,))
                    if r is not None
                }
            )
    else:
        edges.append(
            lambda path: {
                r
                for (r,) in db.execute(_q_forward_refs(), (path,))
                if r != path
            }
        )
        if include_outputs:
            edges.append(
                lambda path: {
                    r
                    for (r,) in db.execute(_q_deriv_outputs(), (path,))
                    if r is not None
                }
            )
        if include_derivers:
            edges.append(
                lambda path: {
                    r
                    for (r,) in db.execute(_q_has_deriver(), (path,))
                    if r is not None
                }
            )

    # ── BFS traversal ────────────────────────────────────────────

    closure = set(start_paths)
    frontier = set(start_paths)

    while frontier:
        next_frontier: set[str] = set()
        for path in frontier:
            for edge_fn in edges:
                neighbors = edge_fn(path)
                new = neighbors - closure
                next_frontier.update(new)
                closure.update(new)
        frontier = next_frontier

    return closure
