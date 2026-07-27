"""``models.StorePath`` reimplements Nix's accessors; this pins them together.

:class:`nanopynix.models.StorePath` is a ``str`` subclass that parses
``hash_part``, ``name`` and ``is_derivation`` in Python rather than asking
libnix. That is deliberate and worth keeping: the rpc client wraps paths in
bulk in a dozen places and ``query_all_valid_paths`` can return an entire
store, so routing construction through ``nix::StorePath`` would cost a
nanobind call per path to re-validate what the worker already validated.

The cost of that choice is a copy of somebody else's logic with nothing
holding the two together. This file is that thing. It compares the Python
accessors against the real ``nix::StorePath`` bindings over inputs chosen to
hit the places a hand-rolled version would plausibly drift:

* a plain name, and a ``.drv`` -- the base case for ``is_derivation``
* a name full of dots (``lib.so.1.2``) -- a naive "contains .drv" or
  "split on the first dot" would get this wrong
* ``x.drv.drv`` and ``x.drv-1`` -- suffix, not substring: Nix checks the
  *end* of the name, so the first is a derivation and the second is not
* a name containing ``-`` -- the hash separator is the *first* one only

Only valid store paths are compared, because the two disagree by design
outside that set: ``nix::StorePath``'s constructor throws ``BadStorePath``
for, say, an empty name, while the Python one has no constructor-time
validation at all and reports ``name == ""``. Pinning that divergence here
would be pinning the absence of validation we deliberately did not add.
"""

from __future__ import annotations

import pytest
from nanopynix_bindings.store import StorePath as RawStorePath

from nanopynix.models import StorePath

# A valid Nix base32 hash. The alphabet omits e/o/u/t, so this is not an
# arbitrary 32-character string -- `RawStorePath` rejects one that is not.
HASH = "a" * 32

NAMES = [
    "hello",
    "hello-2.12.1",
    "hello-2.12.1.drv",
    "lib.so.1.2",
    "x.drv.drv",
    "x.drv-1",
    "name.with.dots.drv",
]

# The two sides are handed different spellings of the same path on purpose:
# `nix::StorePath` parses a basename, `models.StorePath` a full path.
BASE_NAMES = [f"{HASH}-{name}" for name in NAMES]


@pytest.mark.parametrize("base_name", BASE_NAMES)
class TestPythonAccessorsMatchNix:
    """Each accessor, asserted against the binding rather than a literal.

    Written against ``RawStorePath`` rather than expected values on purpose:
    a literal would pin what we *believe* Nix does, which is the same belief
    the Python implementation encodes, so the two could be wrong together.
    """

    def test_hash_part_agrees(self, base_name: str) -> None:
        assert StorePath(f"/nix/store/{base_name}").hash_part == RawStorePath(base_name).hash_part()

    def test_name_agrees(self, base_name: str) -> None:
        assert StorePath(f"/nix/store/{base_name}").name == RawStorePath(base_name).name()

    def test_is_derivation_agrees(self, base_name: str) -> None:
        assert StorePath(f"/nix/store/{base_name}").is_derivation == RawStorePath(base_name).is_derivation()

    def test_the_store_directory_does_not_reach_the_accessors(self, base_name: str) -> None:
        """``base_name`` strips the store dir, which is what makes the rest comparable.

        ``nix::StorePath`` is constructed from a basename and never sees a
        store directory; the Python one is handed a full path and has to strip
        it. A regression there would show up as every other assertion above
        failing at once, so it is stated separately to say which half broke.
        """
        assert StorePath(f"/nix/store/{base_name}").base_name == RawStorePath(base_name).to_string()


def test_the_comparison_would_notice_a_divergence() -> None:
    """Guards the guard: these accessors do disagree on something.

    Without this, a future refactor that made both sides return a constant --
    or a ``RawStorePath`` whose accessors stopped being called -- would leave
    every assertion above passing while comparing nothing. An invalid path is
    the cheapest thing the two provably handle differently.
    """
    with pytest.raises(Exception, match="not a valid store path"):
        RawStorePath(f"{HASH}-")

    assert StorePath(f"/nix/store/{HASH}-").name == ""
