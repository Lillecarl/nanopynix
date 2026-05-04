"""Unit tests for pynixd.derived_path — derived path parsing and serialization.

Tests the DerivedPath union types, SingleDerivedPath, OutputsSpec,
parsing with both `^` and `!` separators, and the DerivedPath str subclass.
All tests are pure — no I/O, no mocking.
"""

from __future__ import annotations

import pytest

from pynixd.derived_path import (
    DerivedPath,
    DerivedPathBuilt,
    DerivedPathOpaque,
    OutputsAll,
    OutputsNames,
    SingleDerivedPathBuilt,
    SingleDerivedPathOpaque,
    dp_drv_path,
    dp_is_nested,
    dp_output_names,
    parse_derived_path,
    parse_derived_path_legacy,
)
from pynixd.store_path import StorePath


class TestOutputsSpec:
    def test_outputs_all_to_string(self):
        assert OutputsAll().to_string() == "*"

    def test_outputs_names_sorted(self):
        spec = OutputsNames(frozenset({"out", "lib", "dev"}))
        assert spec.to_string() == "dev,lib,out"

    def test_outputs_names_single(self):
        spec = OutputsNames(frozenset({"out"}))
        assert spec.to_string() == "out"


class TestSingleDerivedPath:
    def test_opaque(self):
        sdp = SingleDerivedPathOpaque(path=StorePath("/nix/store/abc-foo"))
        assert sdp.to_string() == "/nix/store/abc-foo"
        assert sdp.to_string_legacy() == "/nix/store/abc-foo"
        assert sdp.base_store_path() == StorePath("/nix/store/abc-foo")

    def test_built(self):
        inner = SingleDerivedPathOpaque(path=StorePath("/nix/store/abc-foo.drv"))
        sdp = SingleDerivedPathBuilt(drv_path=inner, output="out")
        assert sdp.to_string() == "/nix/store/abc-foo.drv^out"
        assert sdp.to_string_legacy() == "/nix/store/abc-foo.drv!out"
        assert sdp.base_store_path() == StorePath("/nix/store/abc-foo.drv")

    def test_nested_built(self):
        inner = SingleDerivedPathOpaque(path=StorePath("/nix/store/abc-foo.drv"))
        middle = SingleDerivedPathBuilt(drv_path=inner, output="out")
        top = SingleDerivedPathBuilt(drv_path=middle, output="lib")
        assert top.to_string() == "/nix/store/abc-foo.drv^out^lib"
        assert top.to_string_legacy() == "/nix/store/abc-foo.drv!out!lib"
        assert top.base_store_path() == StorePath("/nix/store/abc-foo.drv")


class TestDerivedPathUnion:
    def test_opaque(self):
        dp = DerivedPathOpaque(path=StorePath("/nix/store/abc-foo"))
        assert dp.to_string() == "/nix/store/abc-foo"
        assert dp.to_string_legacy() == "/nix/store/abc-foo"

    def test_built_with_all_outputs(self):
        inner = SingleDerivedPathOpaque(path=StorePath("/nix/store/abc.drv"))
        dp = DerivedPathBuilt(drv_path=inner, outputs=OutputsAll())
        assert dp.to_string() == "/nix/store/abc.drv^*"
        assert dp.to_string_legacy() == "/nix/store/abc.drv!*"

    def test_built_with_specific_outputs(self):
        inner = SingleDerivedPathOpaque(path=StorePath("/nix/store/abc.drv"))
        dp = DerivedPathBuilt(drv_path=inner, outputs=OutputsNames(frozenset({"out", "lib"})))
        assert dp.to_string() == "/nix/store/abc.drv^lib,out"
        assert dp.to_string_legacy() == "/nix/store/abc.drv!lib,out"

    def test_built_nested(self):

        inner = SingleDerivedPathOpaque(path=StorePath("/nix/store/abc.drv"))
        middle = SingleDerivedPathBuilt(drv_path=inner, output="out")
        dp = DerivedPathBuilt(drv_path=middle, outputs=OutputsAll())
        assert dp.to_string() == "/nix/store/abc.drv^out^*"
        assert dp_is_nested(dp) is True


class TestParseDerivedPath:
    def test_opaque_path(self):
        dp = parse_derived_path("/nix/store/abc-foo")
        assert isinstance(dp, DerivedPathOpaque)
        assert dp.path == StorePath("/nix/store/abc-foo")

    def test_bare_drv_normalized_to_built_all(self):
        dp = parse_derived_path("/nix/store/abc.drv")
        assert isinstance(dp, DerivedPathBuilt)
        assert isinstance(dp.outputs, OutputsAll)
        assert isinstance(dp.drv_path, SingleDerivedPathOpaque)
        assert dp.drv_path.path == StorePath("/nix/store/abc.drv")

    def test_built_with_output(self):
        dp = parse_derived_path("/nix/store/abc.drv^out")
        assert isinstance(dp, DerivedPathBuilt)
        assert isinstance(dp.outputs, OutputsNames)
        assert dp.outputs.names == {"out"}

    def test_nested_built(self):
        dp = parse_derived_path("/nix/store/abc.drv^out^lib")
        assert isinstance(dp, DerivedPathBuilt)
        assert dp_is_nested(dp) is True
        assert isinstance(dp.drv_path, SingleDerivedPathBuilt)
        assert dp.drv_path.output == "out"

    def test_multiple_outputs(self):
        dp = parse_derived_path("/nix/store/abc.drv^out,lib")
        assert isinstance(dp, DerivedPathBuilt)
        assert isinstance(dp.outputs, OutputsNames)
        assert dp.outputs.names == {"out", "lib"}


class TestParseDerivedPathLegacy:
    def test_legacy_built_with_output(self):
        dp = parse_derived_path_legacy("/nix/store/abc.drv!out")
        assert isinstance(dp, DerivedPathBuilt)
        assert isinstance(dp.outputs, OutputsNames)
        assert dp.outputs.names == {"out"}

    def test_legacy_bare_drv(self):
        dp = parse_derived_path_legacy("/nix/store/abc.drv")
        assert isinstance(dp, DerivedPathBuilt)
        assert isinstance(dp.outputs, OutputsAll)


class TestHelperAccessors:
    def test_dp_drv_path_opaque(self):
        dp = DerivedPathOpaque(path=StorePath("/nix/store/abc-foo"))
        assert dp_drv_path(dp) == "/nix/store/abc-foo"

    def test_dp_drv_path_built(self):
        inner = SingleDerivedPathOpaque(path=StorePath("/nix/store/abc.drv"))
        dp = DerivedPathBuilt(drv_path=inner, outputs=OutputsAll())
        assert dp_drv_path(dp) == "/nix/store/abc.drv"

    def test_dp_output_names_opaque(self):
        dp = DerivedPathOpaque(path=StorePath("/nix/store/abc-foo"))
        assert dp_output_names(dp) == set()

    def test_dp_output_names_drv(self):
        dp = DerivedPathOpaque(path=StorePath("/nix/store/abc.drv"))
        assert dp_output_names(dp) == {"*"}

    def test_dp_output_names_built_all(self):
        inner = SingleDerivedPathOpaque(path=StorePath("/nix/store/abc.drv"))
        dp = DerivedPathBuilt(drv_path=inner, outputs=OutputsAll())
        assert dp_output_names(dp) == {"*"}

    def test_dp_output_names_built_specific(self):
        inner = SingleDerivedPathOpaque(path=StorePath("/nix/store/abc.drv"))
        dp = DerivedPathBuilt(drv_path=inner, outputs=OutputsNames(frozenset({"out"})))
        assert dp_output_names(dp) == {"out"}

    def test_dp_is_nested_false_opaque(self):
        dp = DerivedPathOpaque(path=StorePath("/nix/store/abc-foo"))
        assert dp_is_nested(dp) is False

    def test_dp_is_nested_false_single(self):
        inner = SingleDerivedPathOpaque(path=StorePath("/nix/store/abc.drv"))
        dp = DerivedPathBuilt(drv_path=inner, outputs=OutputsAll())
        assert dp_is_nested(dp) is False

    def test_dp_is_nested_true(self):
        inner = SingleDerivedPathOpaque(path=StorePath("/nix/store/abc.drv"))
        middle = SingleDerivedPathBuilt(drv_path=inner, output="out")
        dp = DerivedPathBuilt(drv_path=middle, outputs=OutputsAll())
        assert dp_is_nested(dp) is True


class TestDerivedPathStrSubclass:
    def test_construction_from_opaque(self):
        dp = DerivedPath("/nix/store/abc-foo")
        assert isinstance(dp.derived, DerivedPathOpaque)
        assert dp.drv_path == "/nix/store/abc-foo"
        assert dp.output_names == set()

    def test_construction_from_built_legacy(self):
        dp = DerivedPath("/nix/store/abc.drv!out")
        assert isinstance(dp.derived, DerivedPathBuilt)
        assert dp.drv_path == "/nix/store/abc.drv"
        assert dp.output_names == {"out"}

    def test_construction_from_bare_drv(self):
        dp = DerivedPath("/nix/store/abc.drv")
        assert isinstance(dp.derived, DerivedPathBuilt)
        assert dp.drv_path == "/nix/store/abc.drv"
        assert dp.output_names == {"*"}

    def test_is_nested(self):
        dp = DerivedPath("/nix/store/a.drv!out!lib")
        assert dp.is_nested is True
