from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

_DOCS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_DOCS_DIR))
sys.path.insert(0, str(_DOCS_DIR.parent / "nanopynix" / "src"))
sys.path.insert(0, str(_DOCS_DIR.parent / "pynix" / "src"))
# grpclib-transports is documented on this site too (docs/grpclib-transports),
# and autodoc has to import it. The docs venv already resolves it as a
# dependency of nanopynix, so this only matters when conf.py is read outside
# that venv -- which is what the two lines above are for as well.
sys.path.insert(0, str(_DOCS_DIR.parent / "grpclib-transports" / "src"))

from _generate_pynix_reference import generate as _generate_pynix_reference  # noqa: E402, I001 -- sys.path must be extended first, and this import cannot be merged into the block above it

if TYPE_CHECKING:
    from sphinx.application import Sphinx

project = "nanopynix"
copyright = "2025, Carl Andersson"  # noqa: A001 -- Sphinx conf variable, not the Python builtin
author = "Carl Andersson"

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
]

myst_enable_extensions = [
    "colon_fence",
    "deflist",
]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

html_theme = "furo"

# Disable smart quotes/dashes: this site is full of literal `--flag` mentions
# and numeric ranges (e.g. "0-7") that must not be typographically mangled.
smartquotes = False

# The Nix-built nanopynix-docs package (nix/docs.nix) builds hermetically —
# no network access — so it sets NANOPYNIX_DOCS_OFFLINE to skip intersphinx
# rather than fail trying to fetch docs.python.org's inventory.
intersphinx_mapping = (
    {} if os.environ.get("NANOPYNIX_DOCS_OFFLINE") else {"python": ("https://docs.python.org/3", None)}
)

autodoc_member_order = "bysource"
autodoc_typehints = "description"
autodoc_default_options = {
    "member-order": "bysource",
}
napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_use_rtype = False


def _on_builder_inited(app: Sphinx) -> None:
    del app
    _generate_pynix_reference()


def setup(app: Sphinx) -> None:
    app.connect("builder-inited", _on_builder_inited)
