"""Tests for the validateJSONSchema primop.

Schemas live as real files under ``assets/`` rather than embedded strings, so
the fixtures read the same way any other Nix consumer would point
``validateJSONSchema`` at a schema copied into the store by a fetcher.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanopynix import Session
from nanopynix.primops import jsonschema_primops

pytestmark = pytest.mark.nix_version(minimum="2.32")

_ASSETS = Path(__file__).parent / "assets"
_WIDGET_SCHEMA = _ASSETS / "widget-schema.json"
_MALFORMED_SCHEMA = _ASSETS / "malformed-schema.json"


@pytest.mark.anyio
async def test_valid_value_passes_through_unchanged():
    async with (
        Session(primops=jsonschema_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        v = await eval.string(f'builtins.validateJSONSchema {{ name = "widget"; count = 3; }} {_WIDGET_SCHEMA}')
        assert await v.force_deep() == {"name": "widget", "count": 3}


@pytest.mark.anyio
async def test_invalid_value_raises_with_violation_details():
    async with (
        Session(primops=jsonschema_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        with pytest.raises(Exception) as exc_info:  # noqa: PT011
            v = await eval.string(f"builtins.validateJSONSchema {{ count = -1; }} {_WIDGET_SCHEMA}")
            await v.force_deep()
        message = str(exc_info.value)
        assert "validateJSONSchema: value does not match schema" in message
        assert "'name' is a required property" in message
        assert "count" in message


@pytest.mark.anyio
async def test_malformed_schema_raises_descriptive_error():
    async with (
        Session(primops=jsonschema_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        with pytest.raises(Exception) as exc_info:  # noqa: PT011
            v = await eval.string(f"builtins.validateJSONSchema {{ }} {_MALFORMED_SCHEMA}")
            await v.force_deep()
        message = str(exc_info.value)
        assert "validateJSONSchema: schema file" in message
        assert "is invalid" in message
