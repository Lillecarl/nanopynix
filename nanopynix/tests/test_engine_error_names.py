"""An engine's error class maps by its own name, or by its nearest parent's.

huggorm spells two classes differently from the C++ name, and binds a
subclass the table does not name. Stand-in classes in the engine's module
test the translator without either engine raising anything.
"""

from __future__ import annotations

from nanopynix import exceptions
from nanopynix._engine import ENGINE


def _engine_class(name: str, *bases: type[BaseException]) -> type[BaseException]:
    return type(name, bases or (Exception,), {"__module__": f"{ENGINE}.errors"})


def test_a_huggorm_spelling_maps_to_its_class() -> None:
    root = _engine_class("NixError")
    wrong_type = _engine_class("NixTypeError", _engine_class("EvalError", root))
    translated = exceptions.translate_nix_exception(wrong_type("expected a set but found an integer"))
    assert type(translated) is exceptions.NixTypeError


def test_an_unnamed_subclass_maps_as_its_parent() -> None:
    parent = _engine_class("BadStorePath", _engine_class("NixError"))
    translated = exceptions.translate_nix_exception(_engine_class("BadStorePathName", parent)("bad name"))
    assert type(translated) is exceptions.BadStorePathError


def test_a_class_outside_the_engine_is_not_translated() -> None:
    assert exceptions.translate_nix_exception(TypeError("not ours")) is None
