"""Store models: the round trip as a property, over every field (#18).

``test_stores.py`` covers the same models with hand-written cases. Sixteen of
them, and between them they set 29 of the 171 supported ``(model, field)``
pairs -- so 83% of the fields in this library were rendered by nothing. This
module builds the strategies from ``model_fields`` instead, so every field is
covered without 171 hand-written cases, and a field added tomorrow is covered
the day it appears.

The two properties are the ones the models rest on:

- ``model -> uri() -> parse()`` returns an equal model;
- the URI is stable under a second render.

**No store is opened here.** Neither property needs one, and most of these
configurations name a machine or a bucket that does not exist. ``test_stores.py``
opens the two local models for real; that is the whole of the opening coverage,
and this module adds none.

Four limits are real and are not defects of this library. Each has its own
test below, so the boundary is pinned rather than only avoided by the
strategies:

1. **A list setting cannot hold whitespace.** Nix joins a list with spaces and
   has no escape, so ``["a b"]`` comes back as ``["a", "b"]``.
2. **An empty list is not a value.** It renders to nothing and parses back as
   unset.
3. **The authority must be valid authority syntax.** ``:`` and ``@`` and
   ``[]`` mean something to Nix inside the authority, so they cannot be
   escaped -- a URI that nanopynix could read back would then be a URI Nix
   reads wrongly. Nix refuses ``file://relative``, and normalises ``ssh://:``
   to an empty authority.
4. **A store type with two schemes has two spellings for one of them.**
   Naming ``https`` on an :class:`~nanopynix.stores.HttpBinaryCache` renders
   the same URI as leaving it unset, so the parser returns the unset one.

Everything else goes in unescaped and comes back whole, including a control
character, a NUL and an emoji. That was measured, so the alphabets below
exclude nothing for merely being unusual.
"""

from __future__ import annotations

from types import UnionType
from typing import TYPE_CHECKING, Any, Literal, Union, get_args, get_origin

import pytest
from hypothesis import given, settings, strategies as st

from nanopynix import stores
from nanopynix.settings import field_is_supported
from nanopynix.stores import (
    _uri_part_of,  # type: ignore[reportPrivateUsage] -- the library's own reading of a field's URI role
)
from tests.support.notes import note

if TYPE_CHECKING:
    from collections.abc import Callable

#: ``database=None`` because the packaged CI runner ``cd``s into a read-only
#: store copy of this repository, so Hypothesis has nowhere to write its
#: example database. ``deadline=None`` because the TSan jobs run this same
#: suite under instrumentation, where a per-example deadline measures the
#: sanitiser rather than the code. A render-and-parse cycle takes 0.09 ms
#: uninstrumented, so the budget below is a fraction of a second.
_PROPERTY = settings(max_examples=200, deadline=None, database=None)

#: Every character a setting may hold. A newline, a NUL and an emoji all
#: survive the round trip, so nothing is excluded for being unusual. A lone
#: surrogate is: it is not text, and ``quote`` raises ``UnicodeEncodeError``
#: rather than encoding it, which is the right answer and not a round trip.
_CHARACTER = st.characters(exclude_categories=("Cs",))

#: Any text at all, including the empty string -- which does render, and does
#: come back. See ``test_an_empty_list_setting_is_not_written_at_all``.
_TEXT = st.text(alphabet=_CHARACTER, max_size=10)

#: One element of a list setting. Whitespace is excluded because Nix joins the
#: list with spaces, and this predicate is exactly the one ``str.split`` uses.
_LIST_ITEM = st.text(alphabet=_CHARACTER.filter(lambda char: not char.isspace()), min_size=1, max_size=8)

#: One piece of an authority. ``:@[]`` are excluded because Nix reads them as
#: structure, and ``/`` because it separates the pieces. Everything else --
#: ``&``, ``?``, ``#``, ``%``, a space -- is included on purpose: those are
#: what the authority escaping has to survive.
_AUTHORITY_PIECE = st.text(
    alphabet=_CHARACTER.filter(lambda char: char not in ":@[]/"),
    min_size=1,
    max_size=8,
)

#: An absolute path, for a store whose authority is a directory or a socket.
_PATH_AUTHORITY = st.lists(_AUTHORITY_PIECE, min_size=1, max_size=3).map(lambda parts: "/" + "/".join(parts))

#: A host, in each of the four shapes Nix accepts.
_HOST_AUTHORITY = st.one_of(
    _AUTHORITY_PIECE,
    st.builds("{}@{}".format, _AUTHORITY_PIECE, _AUTHORITY_PIECE),
    st.builds("{}:{}".format, _AUTHORITY_PIECE, st.integers(min_value=1, max_value=65535)),
    st.just("[::1]"),
)

#: Every field that names part of the URI itself, and what that part holds.
#:
#: A judgement, and so a literal: what a store type accepts here is not
#: derivable from the annotation, which is ``str`` for all but one of them.
#: Nix refuses ``file://relative`` and normalises ``ssh://:`` to nothing, so a
#: strategy that ignored the difference would only prove that Nix rejects
#: garbage. The test below holds this table to the fields that exist.
URI_PART_STRATEGIES: dict[tuple[str, str], st.SearchStrategy[str]] = {
    ("Daemon", "socket"): _PATH_AUTHORITY,
    ("FileBinaryCache", "path"): _PATH_AUTHORITY,
    ("Ssh", "host"): _HOST_AUTHORITY,
    ("SshNg", "host"): _HOST_AUTHORITY,
    ("MountedSshNg", "host"): _HOST_AUTHORITY,
    ("HttpBinaryCache", "host"): _HOST_AUTHORITY,
    # A bucket name, which S3 constrains further than a URI does.
    ("S3BinaryCache", "bucket"): _AUTHORITY_PIECE,
    # Only the scheme that is *not* the model's own. Naming the own scheme
    # renders the same URI and parses back unset, so it is a second spelling
    # of one store rather than a second store -- see
    # `test_naming_the_default_scheme_normalises_away`.
    ("HttpBinaryCache", "url_scheme"): st.just("http"),
}


def _scalar_strategy(annotation: Any) -> st.SearchStrategy[Any]:
    """A strategy for one field's annotation, which is always ``T | None``."""
    origin = get_origin(annotation)
    if origin in {UnionType, Union}:
        members = [member for member in get_args(annotation) if member is not type(None)]
        if len(members) != 1:
            raise AssertionError(f"expected one non-None member in {annotation!r}, got {members!r}")
        return _scalar_strategy(members[0])
    if origin is Literal:
        return st.sampled_from(get_args(annotation))
    if origin is list:
        # Never empty: an empty list renders to nothing. See the test below.
        return st.lists(_LIST_ITEM, min_size=1, max_size=3)
    if annotation is bool:
        return st.booleans()
    if annotation is int:
        # Negative values included. Nix would refuse some of them when the
        # store is opened, and this never opens one -- the claim is that the
        # URI carries back what went in.
        return st.integers(min_value=-1000, max_value=100_000)
    if annotation is str:
        return _TEXT
    raise AssertionError(f"no strategy for {annotation!r}; add one when a store model grows a new field type")


def field_strategies(model: type[stores.StoreConfig]) -> dict[str, st.SearchStrategy[Any]]:
    """A strategy per field of ``model`` that the running Nix supports.

    An optional field also generates ``None``, which is how a configuration
    that sets only some of its settings gets covered.
    """
    strategies: dict[str, st.SearchStrategy[Any]] = {}
    for name, field in model.model_fields.items():
        if not field_is_supported(field):
            continue
        uri_part = URI_PART_STRATEGIES.get((model.__name__, name))
        value = uri_part if uri_part is not None else _scalar_strategy(field.annotation)
        strategies[name] = value if field.is_required() else st.one_of(st.none(), value)
    return strategies


def model_strategy(model: type[stores.StoreConfig]) -> st.SearchStrategy[stores.StoreConfig]:
    """Instances of ``model``, with every supported field reachable."""
    return st.builds(model, **field_strategies(model))


def _supported_fields(model: type[stores.StoreConfig]) -> set[str]:
    return {name for name, field in model.model_fields.items() if field_is_supported(field)}


def _uri_part_fields() -> set[tuple[str, str]]:
    """Every ``(model, field)`` pair that names part of the URI itself."""
    found: set[tuple[str, str]] = set()
    for model in stores.STORE_MODELS:
        for name, field in model.model_fields.items():
            # The library's own accessor, not a second reading of
            # `json_schema_extra`. This test asks whether the table below
            # names the fields that exist, and "which fields are URI parts" is
            # `stores`' answer to give.
            if _uri_part_of(field) is not None:
                found.add((model.__name__, name))
    return found


# ── Guards: the strategies must reach what they claim to reach ───────


def test_the_strategies_cover_every_supported_field() -> None:
    """A strategy exists for each field, so the properties test all of them.

    Without this, a field the builder quietly skipped would be exercised by
    nothing and both properties below would still pass. That silent no-op is
    the failure this file has to rule out first.
    """
    total = 0
    for model in stores.STORE_MODELS:
        supported = _supported_fields(model)
        assert supported, f"{model.__name__} has no supported fields, so the version gate reads wrong"
        assert set(field_strategies(model)) == supported, f"{model.__name__}: the strategies and the fields disagree"
        total += len(supported)
    note(supported_field_pairs=total)
    assert total > 100, f"only {total} model/field pairs; a whole model or its base classes went missing"


def test_the_uri_part_table_names_the_uri_part_fields() -> None:
    """``URI_PART_STRATEGIES`` and the models agree, in both directions.

    A new authority or scheme field fails here until someone says what shape
    it holds. An entry whose field is gone fails too, so the table cannot rot.
    """
    assert set(URI_PART_STRATEGIES) == _uri_part_fields()


# ── The two properties ───────────────────────────────────────────────


@pytest.mark.parametrize("model", stores.STORE_MODELS, ids=lambda model: model.__name__)
def test_a_model_survives_being_written_as_a_uri(model: type[stores.StoreConfig]) -> None:
    """``model -> uri() -> parse()`` is the identity, for every model.

    Parametrised over the models rather than choosing one inside the strategy,
    so each model gets its own example budget. Sampling from eleven models
    would give the widest one, ``S3BinaryCache`` with 29 fields, the same
    handful of examples as ``Auto`` with six.
    """
    checked: list[str] = []

    @_PROPERTY
    @given(config=model_strategy(model))
    def check(config: stores.StoreConfig) -> None:
        uri = config.uri()
        checked.append(uri)
        reparsed = stores.parse(uri)
        assert type(reparsed) is model
        assert reparsed == config, f"{uri!r} did not parse back to the configuration that wrote it"

    check()
    note(**{f"examples/{model.__name__}": len(checked)})


@pytest.mark.parametrize("model", stores.STORE_MODELS, ids=lambda model: model.__name__)
def test_a_uri_is_stable_under_a_second_render(model: type[stores.StoreConfig]) -> None:
    """``parse(uri).uri() == uri``, for every URI these models write.

    The corpus is rendered rather than generated character by character. A
    generated string is a store URI only by accident, so that strategy would
    spend its budget proving that Nix rejects nonsense.

    Separate from the property above because it fails differently. A model can
    parse back equal while the second render reorders or re-escapes something,
    and then a URI that a caller stored is not the URI they get next time.
    """

    @_PROPERTY
    @given(config=model_strategy(model))
    def check(config: stores.StoreConfig) -> None:
        uri = config.uri()
        assert stores.parse(uri).uri() == uri

    check()


# ── The three limits, pinned ─────────────────────────────────────────


def test_a_list_setting_cannot_hold_whitespace() -> None:
    """Nix joins a list with spaces, and there is no escape.

    This is why ``_LIST_ITEM`` excludes whitespace. Stated as a test so the
    exclusion is a documented limit of Nix's encoding rather than a strategy
    that quietly avoids a failure.
    """
    config = stores.Local(system_features=["big parallel"])
    reparsed = stores.parse(config.uri())
    assert isinstance(reparsed, stores.Local)
    assert reparsed.system_features == ["big", "parallel"], "the space stopped splitting the value"


def test_an_empty_list_setting_is_not_written_at_all() -> None:
    """``[]`` renders to nothing, so it comes back unset rather than empty.

    An empty string does render, and does come back. The two are asymmetric on
    purpose: an empty string is a value Nix can hold, and an empty list is the
    absence of every value.
    """
    empty_list = stores.Local(system_features=[])
    assert "system-features" not in empty_list.params()
    assert stores.parse(empty_list.uri()) == stores.Local()

    empty_string = stores.Local(root="")
    assert empty_string.params() == {"root": ""}
    assert stores.parse(empty_string.uri()) == empty_string


@pytest.mark.parametrize(
    "path",
    ["/var/cache/a&b", "/var/cache/a?b", "/var/cache/a#b", "/var/cache/a b", "/var/cache/a%b", "/var/cache/a%26b"],
    ids=["ampersand", "question", "hash", "space", "percent", "encoded-ampersand"],
)
def test_the_authority_is_escaped_like_a_parameter(path: str) -> None:
    """A cache directory is a path, and a path may hold a URI metacharacter.

    Every one of these was broken until this test was written, and each broke
    differently. ``&`` ended the authority and made the rest a query
    parameter, which Nix then warned about and dropped. ``?`` and ``#``
    truncated the path silently. A space and a bare ``%`` made Nix refuse the
    whole URI.

    Nix does not unescape the authority, unlike the parameters, so
    :func:`~nanopynix.stores.parse` unescapes it. ``%26`` in the last case is
    therefore a literal six characters going in and coming out.
    """
    config = stores.FileBinaryCache(path=path)
    uri = config.uri()
    note(**{f"uri/{path}": uri})

    reparsed = stores.parse(uri)
    assert isinstance(reparsed, stores.FileBinaryCache)
    assert reparsed.path == path
    assert reparsed.uri() == uri


@pytest.mark.parametrize(
    "authority",
    ["user@host", "host:22", "[::1]", "user@host:22"],
    ids=["user", "port", "ipv6", "user-and-port"],
)
def test_the_structural_characters_of_an_authority_are_left_alone(authority: str) -> None:
    """``:@[]`` reach Nix unescaped, because Nix reads them.

    The other half of the escaping rule. Escaping these would give a URI that
    nanopynix reads back correctly and Nix reads as a host literally named
    ``user%40host``, which is the worse of the two failures because nothing
    reports it.
    """
    config = stores.Ssh(host=authority)
    uri = config.uri()
    assert uri == f"ssh://{authority}", "an authority character Nix reads must not be escaped"
    assert stores.parse(uri) == config


@pytest.mark.parametrize(
    ("make", "rendered"),
    [
        (lambda: stores.FileBinaryCache(path="relative"), "file://relative"),
        (lambda: stores.HttpBinaryCache(host="["), "https://["),
    ],
    ids=["relative-path", "unterminated-ipv6"],
)
def test_nix_refuses_an_authority_that_is_not_authority_syntax(
    make: Callable[[], stores.StoreConfig],
    rendered: str,
) -> None:
    """The limit that shapes ``URI_PART_STRATEGIES``, written down.

    Neither can be fixed by escaping, because the characters that break them
    are the characters Nix needs unescaped. Nix refuses each one rather than
    accepting it and meaning something else, which is the right outcome and is
    what makes the strategies' narrower alphabet honest.
    """
    config = make()
    assert config._render_raw() == rendered
    with pytest.raises(Exception, match="Cannot parse Nix store") as excinfo:
        config.uri()
    note(**{f"refused/{rendered}": type(excinfo.value).__name__})


def test_an_authority_that_is_only_a_separator_is_normalised_away() -> None:
    """``ssh://:`` is not refused. Nix drops the colon and leaves nothing.

    The third shape ``URI_PART_STRATEGIES`` has to avoid, and the one that
    matters most, because it is the one Nix accepts. A host of ``":"`` is not
    a host, so losing it is reasonable -- but it is a loss, and a property
    that generated it would report it as a round-trip failure of this library.
    """
    assert stores.Ssh(host=":").uri() == "ssh://"
    assert stores.parse("ssh://") == stores.Ssh(host="")

    assert stores.Daemon(socket=":").uri() == "unix://"
    assert stores.parse("unix://") == stores.Daemon(), "an optional authority comes back unset, not empty"


def test_naming_the_default_scheme_normalises_away() -> None:
    """``url_scheme="https"`` and leaving it unset are one store, not two.

    Both render ``https://``, so the parser cannot tell them apart and returns
    the unset spelling. This is normalisation, like the parameter order Nix
    imposes, and it is why ``URI_PART_STRATEGIES`` generates only ``"http"``.
    """
    explicit = stores.HttpBinaryCache(url_scheme="https", host="cache.example.com")
    implicit = stores.HttpBinaryCache(host="cache.example.com")
    assert explicit.uri() == implicit.uri() == "https://cache.example.com"
    assert stores.parse(explicit.uri()) == implicit

    # The other scheme is not the default, so it survives.
    other = stores.HttpBinaryCache(url_scheme="http", host="cache.example.com")
    assert stores.parse(other.uri()) == other
