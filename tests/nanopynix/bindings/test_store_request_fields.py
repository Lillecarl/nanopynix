"""The dict-shaped store entrypoints report missing fields as Nix usage errors.

Every ``store_*`` binding takes a proto-shaped dict. Reading a field used to go
straight through ``nb::dict``'s ``operator[]``, so a missing key surfaced as a
bare Python ``KeyError`` naming only the key -- not the operation that rejected
it, and not one of nanopynix's error types, which made it indistinguishable
from a bug in the caller's own code. It also applied to *optional* fields, so a
hand-built dict could not omit ``include_derivers`` or ``censor`` even though
both have defaults.

These tests pin both halves: required fields raise ``UsageError`` naming the
field, and optional fields fall back to exactly the default the direct API
declares. The second half is what keeps the two APIs from drifting into
disagreeing about what "unset" means.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from nanopynix_bindings import errors as nanopynix_errors

if TYPE_CHECKING:
    from collections.abc import Callable

# (method, an otherwise-complete request, the field left out of it)
REQUIRED_FIELDS: list[tuple[str, dict[str, Any], str]] = [
    ("store_is_valid_path", {}, "path"),
    ("store_parse_store_path", {}, "path"),
    ("store_query_path_info", {}, "path"),
    ("store_query_referrers", {}, "path"),
    ("store_query_valid_derivers", {}, "path"),
    ("store_query_derivation_outputs", {}, "path"),
    ("store_compute_fs_closure", {}, "path"),
    ("store_read_derivation", {}, "path"),
    ("store_get_build_log", {}, "path"),
    ("store_ensure_path", {}, "path"),
    ("store_add_temp_root", {}, "path"),
    ("store_add_indirect_root", {}, "path"),
    ("store_follow_links_to_store_path", {}, "path"),
    ("store_query_path_from_hash_part", {}, "hash_part"),
    ("store_query_substitutable_paths", {}, "paths"),
    ("store_query_missing", {}, "derived_paths"),
    ("store_build_paths_with_results", {}, "derived_paths"),
    ("store_add_perm_root", {"gc_root": "/tmp/unused"}, "store_path"),
    ("store_collect_garbage", {}, "action"),
]


@pytest.mark.parametrize(("method", "request_dict", "field"), REQUIRED_FIELDS)
def test_missing_required_field_raises_usage_error(
    store: Any, method: str, request_dict: dict[str, Any], field: str
) -> None:
    """A missing required field names both the field and the operation."""
    with pytest.raises(nanopynix_errors.UsageError) as excinfo:
        getattr(store, method)(request_dict)

    message = str(excinfo.value)
    assert field in message, f"{method} did not name the missing field: {message}"
    assert method in message, f"{method} did not name itself: {message}"


def test_missing_required_field_is_not_a_bare_key_error(store: Any) -> None:
    """Guard the specific regression: KeyError is not a nanopynix error type.

    ``UsageError`` derives from nanopynix's ``Error``; ``KeyError`` does not,
    so a caller catching nanopynix errors used to miss this entirely.
    """
    with pytest.raises(nanopynix_errors.UsageError) as excinfo:
        store.store_query_referrers({})

    assert not isinstance(excinfo.value, KeyError)
    assert isinstance(excinfo.value, nanopynix_errors.Error)


# (method, request omitting the optional field, equivalent direct-API call)
OPTIONAL_FIELD_DEFAULTS: list[tuple[str, dict[str, Any], str, Callable[[Any], Any]]] = [
    ("store_get_uri", {}, "with_params", lambda s: {"uri": s.get_uri(False)}),
    ("store_find_roots", {}, "censor", lambda s: {"roots": s.find_roots(True)}),
    ("store_verify_store", {}, "check_contents", lambda s: {"errors": s.verify_store(False, False)}),
]


@pytest.mark.parametrize(("method", "request_dict", "field", "direct"), OPTIONAL_FIELD_DEFAULTS)
def test_omitted_optional_field_matches_the_direct_api_default(
    store: Any, method: str, request_dict: dict[str, Any], field: str, direct: Callable[[Any], Any]
) -> None:
    """Omitting an optional field gives the same answer the direct API's default does."""
    del field  # named in the parametrisation for readable test ids
    assert getattr(store, method)(request_dict) == direct(store)


def test_optional_closure_flags_may_all_be_omitted(store: Any, store_seeded_path: Any) -> None:
    """compute_fs_closure's three bool flags default to False, as the direct API declares.

    This is the case that first exposed the bug: the request carries a valid
    ``path`` and nothing else, which is exactly what a caller who wants the
    plain forward closure would write.
    """
    path = str(store_seeded_path)

    assert store.store_compute_fs_closure({"path": path}) == {
        "paths": store.compute_fs_closure(store_seeded_path, False, False, False)
    }
