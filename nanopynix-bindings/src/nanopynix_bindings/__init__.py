"""nanopynix compiled bindings namespace package."""

from __future__ import annotations

# `errors` first, and deliberately so: importing it is what installs the single
# C++ -> Python exception translator, and nothing that can raise a Nix error
# should be importable before it. The translator owns the whole nix::Error
# hierarchy on its own, so unlike the per-type translators it replaced, this
# ordering is belt-and-braces rather than load-bearing correctness.
from . import errors as errors
from . import expr as expr
from . import fetchers as fetchers
from . import flake as flake
from . import main as main
from . import store as store
from . import util as util
