"""Tests for pydantic models — no Nix/C++ dependency."""

import pytest
from pydantic import ValidationError

from nanopynix.models import (
    BuildResult,
    Derivation,
    FlakeRef,
    Input,
    LockedFlake,
    LockedInput,
    LogEvent,
    MissingInfo,
    PathInfo,
    StorePath,
)


# ── StorePath ────────────────────────────────────────────────────────

class TestStorePath:
    def test_basic(self):
        sp = StorePath(
            hash_part="abc123def456",
            name="hello-2.12.1",
            to_string="abc123def456-hello-2.12.1",
        )
        assert sp.hash_part == "abc123def456"
        assert sp.name == "hello-2.12.1"
        assert not sp.is_derivation

    def test_is_derivation(self):
        sp = StorePath(
            hash_part="abc123def456",
            name="hello-2.12.1.drv",
            to_string="abc123def456-hello-2.12.1.drv",
        )
        assert sp.is_derivation

    def test_missing_required_field(self):
        with pytest.raises(ValidationError):
            StorePath(hash_part="abc")

    def test_validate_dict(self):
        sp = StorePath.model_validate({
            "hash_part": "abc123def456",
            "name": "bash-5.2",
            "to_string": "abc123def456-bash-5.2",
        })
        assert sp.name == "bash-5.2"
        assert not sp.is_derivation

    def test_construct_from_string(self):
        sp = StorePath("abc123def456-bash-5.2")
        assert sp.hash_part == "abc123def456"
        assert sp.name == "bash-5.2"
        assert sp.to_string == "abc123def456-bash-5.2"


# ── PathInfo ─────────────────────────────────────────────────────────

class TestPathInfo:
    def test_full(self):
        pi = PathInfo(
            path=StorePath(hash_part="a", name="p", to_string="a-p"),
            nar_hash="sha256:abc",
            nar_size=1234,
            registration_time=1700000000,
            deriver=StorePath(hash_part="b", name="p.drv", to_string="b-p.drv"),
            references=[StorePath(hash_part="c", name="dep", to_string="c-dep")],
            ca="fixed:r:sha256:...",
            ultimate=True,
        )
        assert pi.path.name == "p"
        assert pi.deriver.name == "p.drv"
        assert len(pi.references) == 1

    def test_minimal(self):
        pi = PathInfo(
            path=StorePath(hash_part="a", name="p", to_string="a-p"),
            nar_hash="sha256:abc",
            nar_size=0,
        )
        assert pi.deriver is None
        assert pi.references == []
        assert pi.ca is None
        assert not pi.ultimate

    def test_validate_dict_minimal(self):
        pi = PathInfo.model_validate({
            "path": {"hash_part": "a", "name": "p", "to_string": "a-p"},
            "nar_hash": "sha256:abc",
            "nar_size": 0,
        })
        assert isinstance(pi.path, StorePath)
        assert pi.nar_size == 0


# ── BuildResult ──────────────────────────────────────────────────────

class TestBuildResult:
    def test_success(self):
        br = BuildResult(drv_path="/nix/store/x.drv", success=True, status="built")
        assert br.success
        assert br.error_msg == ""

    def test_failure(self):
        br = BuildResult(
            drv_path="/nix/store/x.drv",
            success=False,
            status="permanent-failure",
            error_msg="compilation failed",
        )
        assert not br.success
        assert br.error_msg == "compilation failed"

    def test_validate_dict(self):
        br = BuildResult.model_validate({
            "drv_path": "/nix/store/x.drv",
            "success": False,
            "status": "timed-out",
        })
        assert br.success is False


# ── MissingInfo ──────────────────────────────────────────────────────

class TestMissingInfo:
    def test_defaults(self):
        mi = MissingInfo()
        assert mi.will_build == []
        assert mi.download_size == 0
        assert mi.nar_size == 0


# ── Input ────────────────────────────────────────────────────────────

class TestInput:
    def test_github(self):
        inp = Input(attrs={"type": "github", "owner": "NixOS", "repo": "nixpkgs", "ref": "nixos-24.11"})
        assert inp.attrs["type"] == "github"

    def test_indirect(self):
        inp = Input(attrs={"type": "indirect", "id": "nixpkgs"})
        assert inp.attrs["id"] == "nixpkgs"

    def test_empty_default(self):
        inp = Input()
        assert inp.attrs == {}


# ── FlakeRef ─────────────────────────────────────────────────────────

class TestFlakeRef:
    def test_with_subdir(self):
        fr = FlakeRef(attrs={
            "type": "github", "owner": "NixOS", "repo": "nixpkgs",
            "ref": "nixos-24.11", "dir": "lib",
        })
        assert fr.attrs["dir"] == "lib"

    def test_basic(self):
        fr = FlakeRef(attrs={"type": "github", "owner": "NixOS", "repo": "nixpkgs"})
        assert fr.attrs["repo"] == "nixpkgs"

    def test_default(self):
        fr = FlakeRef()
        assert fr.attrs == {}


# ── LockedInput / LockedFlake ────────────────────────────────────────

class TestLockedInput:
    def test_with_direct_ref(self):
        li = LockedInput(
            attrs={"type": "github", "owner": "NixOS", "repo": "nixpkgs", "ref": "nixos-24.11", "rev": "abc123"},
            is_flake=True,
        )
        assert li.attrs is not None
        assert li.attrs["rev"] == "abc123"
        assert li.is_flake
        assert li.follows == []

    def test_follows_another_input(self):
        li = LockedInput(follows=["nixpkgs", "flake-utils"])
        assert li.attrs is None
        assert li.follows == ["nixpkgs", "flake-utils"]

    def test_not_a_flake(self):
        li = LockedInput(attrs={"type": "tarball", "url": "https://..."}, is_flake=False)
        assert not li.is_flake

    def test_defaults(self):
        li = LockedInput()
        assert li.attrs is None
        assert li.is_flake is True
        assert li.follows == []


class TestLockedFlake:
    def test_full(self):
        lf = LockedFlake(
            description="A test flake",
            inputs={
                "nixpkgs": LockedInput(
                    attrs={"type": "github", "owner": "NixOS", "repo": "nixpkgs", "rev": "abc"},
                    is_flake=True,
                ),
                "utils": LockedInput(
                    attrs={"type": "github", "owner": "x", "repo": "y"},
                    is_flake=False,
                ),
            },
        )
        assert lf.description == "A test flake"
        assert lf.inputs["nixpkgs"].attrs["rev"] == "abc"
        assert lf.inputs["utils"].is_flake is False

    def test_validate_dict_nested(self):
        lf = LockedFlake.model_validate({
            "description": "hello",
            "inputs": {
                "nixpkgs": {
                    "attrs": {"type": "github", "owner": "NixOS", "repo": "nixpkgs", "rev": "abc"},
                    "is_flake": True,
                },
            },
        })
        assert isinstance(lf.inputs["nixpkgs"], LockedInput)
        assert lf.inputs["nixpkgs"].attrs["rev"] == "abc"


# ── LogEvent ─────────────────────────────────────────────────────────

class TestLogEvent:
    def test_msg_event(self):
        ev = LogEvent(request_id=42, action="msg", args=[3, "evaluating file"])
        assert ev.request_id == 42
        assert ev.action == "msg"
        assert ev.args[0] == 3  # lvlInfo

    def test_start_event(self):
        ev = LogEvent(request_id=1, action="start", args=[1, 3, 100, "building", [], 0])
        assert ev.request_id == 1
        assert ev.action == "start"

    def test_defaults(self):
        ev = LogEvent(action="msg")
        assert ev.request_id == 0
        assert ev.args == []

    def test_with_result_type(self):
        ev = LogEvent(request_id=5, action="result", args=[1, 107], result_type=107)
        assert ev.request_id == 5
        assert ev.action == "result"
        assert ev.result_type == 107


class TestResultType:
    def test_values_match_nix(self):
        from nanopynix.models import ResultType

        assert ResultType.file_linked == 100
        assert ResultType.build_log_line == 101
        assert ResultType.untrusted_path == 102
        assert ResultType.corrupted_path == 103
        assert ResultType.set_phase == 104
        assert ResultType.progress == 105
        assert ResultType.set_expected == 106
        assert ResultType.post_build_log_line == 107
        assert ResultType.fetch_status == 108

    def test_is_int_enum(self):
        from nanopynix.models import ResultType

        assert isinstance(ResultType.corrupted_path, int)
        assert isinstance(ResultType.corrupted_path, ResultType)


class TestStorePathEdge:
    def test_is_derivation_drv_extension(self):
        sp = StorePath(hash_part="a" * 32, name="foo.drv", to_string="a" * 32 + "-foo.drv")
        assert sp.is_derivation is True

    def test_is_derivation_drv_in_middle(self):
        """Only names ending with .drv are derivations."""
        sp = StorePath(hash_part="a" * 32, name="foo.drv.bar", to_string="a" * 32 + "-foo.drv.bar")
        assert sp.is_derivation is False

    def test_is_derivation_no_drv(self):
        sp = StorePath(hash_part="a" * 32, name="bash-5.2", to_string="a" * 32 + "-bash-5.2")
        assert sp.is_derivation is False


def test_derivation_mutable_defaults_isolated():
    """Two Derivation instances do not share mutable defaults (D2 fix)."""
    d1 = Derivation(name="a", system="x86_64-linux", builder="/bin/sh")
    d2 = Derivation(name="b", system="x86_64-linux", builder="/bin/sh")

    d1.args.append("--flag")
    d1.env["VAR"] = "val"
    d1.input_srcs.append("/nix/store/aaa")

    assert d2.args == []
    assert d2.env == {}
    assert d2.input_srcs == []


def test_derivation_accepts_nix_field_aliases():
    drv = Derivation.model_validate({
        "name": "foo",
        "platform": "x86_64-linux",
        "builder": "/bin/sh",
        "env": [["A", "1"]],
        "inputSrcs": ["/nix/store/src"],
        "inputDrvs": [
            {
                "path": "/nix/store/input.drv",
                "outputs": ["out"],
                "children": {"dev": ["out"]},
            }
        ],
    })

    assert drv.system == "x86_64-linux"
    assert drv.env == {"A": "1"}
    assert drv.input_srcs == ["/nix/store/src"]
    assert drv.input_drvs["/nix/store/input.drv"].outputs == ["out"]
    assert drv.input_drvs["/nix/store/input.drv"].dynamic_outputs == {"dev": "out"}
