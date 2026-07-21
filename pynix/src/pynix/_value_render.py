"""Shared pretty-printing for evaluated Nix values, used by pynix's REPL and language server."""

from __future__ import annotations

import json


def format_json(data: object) -> str:
    """Render *data* the same way everywhere pynix prints an evaluated value."""
    return json.dumps(data, indent=2, sort_keys=True)
