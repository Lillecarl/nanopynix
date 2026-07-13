"""Tests for pydantic models — no Nix/C++ dependency."""

from nanopynix_proto.nix.common import AttrsMap, AttrsValue

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
    ResultType,
    StorePath,
)

# ── StorePath ────────────────────────────────────────────────────────


class TestStorePath:
    def test_basic(self):
        sp = StorePath("/nix/store/" + "a" * 32 + "-hello-2.12.1")
        assert isinstance(sp, str)
        assert sp.base_name == "a" * 32 + "-hello-2.12.1"
        assert sp.hash_part == "a" * 32
        assert sp.name == "hello-2.12.1"
        assert str(sp) == "/nix/store/" + "a" * 32 + "-hello-2.12.1"
        assert not sp.is_derivation

    def test_is_derivation(self):
        sp = StorePath("a" * 32 + "-hello-2.12.1.drv")
        assert sp.is_derivation

    def test_construct_from_string(self):
        sp = StorePath("a" * 32 + "-bash-5.2")
        assert sp.hash_part == "a" * 32
        assert sp.name == "bash-5.2"
        assert str(sp) == "a" * 32 + "-bash-5.2"


# ── PathInfo ─────────────────────────────────────────────────────────


class TestPathInfo:
    def test_full(self):
        pi = PathInfo(
            path="/nix/store/" + "a" * 32 + "-p",
            nar_hash="sha256:abc",
            nar_size=1234,
            registration_time=1700000000,
            deriver="/nix/store/" + "b" * 32 + "-p.drv",
            references=["/nix/store/" + "c" * 32 + "-dep"],
            ca="fixed:r:sha256:...",
            ultimate=True,
        )
        path = pi.path
        assert path is not None
        assert StorePath(path).name == "p"
        assert pi.deriver is not None
        deriver = pi.deriver
        assert deriver is not None
        assert StorePath(deriver).name == "p.drv"
        assert len(pi.references) == 1

    def test_minimal(self):
        pi = PathInfo(
            path="/nix/store/" + "a" * 32 + "-p",
            nar_hash="sha256:abc",
            nar_size=0,
        )
        assert pi.deriver is None
        assert pi.references == []
        assert pi.ca is None
        assert not pi.ultimate

    def test_from_dict_minimal(self):
        pi = PathInfo.from_dict(
            {
                "path": "/nix/store/" + "a" * 32 + "-p",
                "nar_hash": "sha256:abc",
                "nar_size": 0,
            }
        )
        assert isinstance(pi.path, str)
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

    def test_from_dict(self):
        br = BuildResult.from_dict(
            {
                "drv_path": "/nix/store/x.drv",
                "success": False,
                "status": "timed-out",
            }
        )
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
        inp = Input(
            attrs={
                "type": AttrsValue(string_value="github"),
                "owner": AttrsValue(string_value="NixOS"),
                "repo": AttrsValue(string_value="nixpkgs"),
                "ref": AttrsValue(string_value="nixos-24.11"),
            }
        )
        assert inp.attrs["type"].string_value == "github"

    def test_indirect(self):
        inp = Input(
            attrs={
                "type": AttrsValue(string_value="indirect"),
                "id": AttrsValue(string_value="nixpkgs"),
            }
        )
        assert inp.attrs["id"].string_value == "nixpkgs"

    def test_empty_default(self):
        inp = Input()
        assert inp.attrs == {}


# ── FlakeRef ─────────────────────────────────────────────────────────


class TestFlakeRef:
    def test_with_subdir(self):
        fr = FlakeRef(
            attrs={
                "type": AttrsValue(string_value="github"),
                "owner": AttrsValue(string_value="NixOS"),
                "repo": AttrsValue(string_value="nixpkgs"),
                "ref": AttrsValue(string_value="nixos-24.11"),
                "dir": AttrsValue(string_value="lib"),
            }
        )
        assert fr.attrs["dir"].string_value == "lib"

    def test_basic(self):
        fr = FlakeRef(
            attrs={
                "type": AttrsValue(string_value="github"),
                "owner": AttrsValue(string_value="NixOS"),
                "repo": AttrsValue(string_value="nixpkgs"),
            }
        )
        assert fr.attrs["repo"].string_value == "nixpkgs"

    def test_default(self):
        fr = FlakeRef()
        assert fr.attrs == {}


# ── LockedInput / LockedFlake ────────────────────────────────────────


class TestLockedInput:
    def test_with_direct_ref(self):
        li = LockedInput(
            attrs=AttrsMap(
                entries={
                    "type": AttrsValue(string_value="github"),
                    "owner": AttrsValue(string_value="NixOS"),
                    "repo": AttrsValue(string_value="nixpkgs"),
                    "ref": AttrsValue(string_value="nixos-24.11"),
                    "rev": AttrsValue(string_value="abc123"),
                }
            ),
            is_flake=True,
        )
        assert li.attrs is not None
        assert li.attrs.entries["rev"].string_value == "abc123"
        assert li.is_flake
        assert li.follows == []

    def test_follows_another_input(self):
        li = LockedInput(follows=["nixpkgs", "flake-utils"])
        assert li.attrs is None
        assert li.follows == ["nixpkgs", "flake-utils"]

    def test_not_a_flake(self):
        li = LockedInput(
            attrs=AttrsMap(
                entries={
                    "type": AttrsValue(string_value="tarball"),
                    "url": AttrsValue(string_value="https://..."),
                }
            ),
            is_flake=False,
        )
        assert not li.is_flake

    def test_defaults(self):
        li = LockedInput()
        assert li.attrs is None
        assert li.is_flake is False  # proto default
        assert li.follows == []


class TestLockedFlake:
    def test_full(self):
        lf = LockedFlake(
            handle=1,
            description="A test flake",
            inputs={
                "nixpkgs": LockedInput(
                    attrs=AttrsMap(
                        entries={
                            "type": AttrsValue(string_value="github"),
                            "owner": AttrsValue(string_value="NixOS"),
                            "repo": AttrsValue(string_value="nixpkgs"),
                            "rev": AttrsValue(string_value="abc"),
                        }
                    ),
                    is_flake=True,
                ),
                "utils": LockedInput(
                    attrs=AttrsMap(
                        entries={
                            "type": AttrsValue(string_value="github"),
                            "owner": AttrsValue(string_value="x"),
                            "repo": AttrsValue(string_value="y"),
                        }
                    ),
                    is_flake=False,
                ),
            },
        )
        assert lf.handle == 1
        assert lf.description == "A test flake"
        assert lf.inputs["nixpkgs"].attrs is not None
        assert lf.inputs["nixpkgs"].attrs.entries["rev"].string_value == "abc"
        assert lf.inputs["utils"].is_flake is False

    def test_construct_nested(self):
        lf = LockedFlake(
            handle=42,
            description="hello",
            inputs={
                "nixpkgs": LockedInput(
                    attrs=AttrsMap(
                        entries={
                            "type": AttrsValue(string_value="github"),
                            "owner": AttrsValue(string_value="NixOS"),
                            "repo": AttrsValue(string_value="nixpkgs"),
                            "rev": AttrsValue(string_value="abc"),
                        }
                    ),
                    is_flake=True,
                ),
            },
        )
        assert lf.handle == 42
        assert isinstance(lf.inputs["nixpkgs"], LockedInput)
        assert lf.inputs["nixpkgs"].attrs is not None
        assert lf.inputs["nixpkgs"].attrs.entries["rev"].string_value == "abc"


# ── LogEvent ─────────────────────────────────────────────────────────


class TestLogEvent:
    def test_msg_event(self):
        ev = LogEvent(request_id=42, action="msg", args=[3, "evaluating file"])
        assert ev.request_id == 42
        assert ev.action == "msg"
        assert ev.args[0] == 3  # lvlInfo
        assert ev.message == "evaluating file"
        assert ev.message_without_ansi == "evaluating file"

    def test_msg_event_ansi_helpers_preserve_raw_and_newlines(self):
        ev = LogEvent(request_id=42, action="msg", args=[3, "\x1b[31mline 1\x1b[0m\nline 2"])

        clean = ev.without_ansi()

        assert ev.message == "\x1b[31mline 1\x1b[0m\nline 2"
        assert ev.args[1] == "\x1b[31mline 1\x1b[0m\nline 2"
        assert ev.message_without_ansi == "line 1\nline 2"
        assert clean.args == [3, "line 1\nline 2"]
        assert clean.message == "line 1\nline 2"

    def test_start_event(self):
        ev = LogEvent(request_id=1, action="start", args=[1, 3, 100, "building", [], 0])
        assert ev.request_id == 1
        assert ev.action == "start"
        assert ev.message is None
        assert ev.message_without_ansi is None

    def test_defaults(self):
        ev = LogEvent(action="msg")
        assert ev.request_id == 0
        assert ev.args == []
        assert ev.message is None

    def test_with_result_type(self):
        ev = LogEvent(request_id=5, action="result", args=[1, 107], result_type=ResultType.POST_BUILD_LOG_LINE)
        assert ev.request_id == 5
        assert ev.action == "result"
        assert ev.result_type == ResultType.POST_BUILD_LOG_LINE


class TestResultType:
    def test_values_match_nix(self):
        from nanopynix.models import ResultType

        assert ResultType.FILE_LINKED == 100
        assert ResultType.BUILD_LOG_LINE == 101
        assert ResultType.UNTRUSTED_PATH == 102
        assert ResultType.CORRUPTED_PATH == 103
        assert ResultType.SET_PHASE == 104
        assert ResultType.PROGRESS == 105
        assert ResultType.SET_EXPECTED == 106
        assert ResultType.POST_BUILD_LOG_LINE == 107
        assert ResultType.FETCH_STATUS == 108

    def test_is_int_enum(self):
        from nanopynix.models import ResultType

        assert isinstance(ResultType.CORRUPTED_PATH, int)
        assert isinstance(ResultType.CORRUPTED_PATH, ResultType)


class TestStorePathEdge:
    def test_is_derivation_drv_extension(self):
        sp = StorePath("a" * 32 + "-foo.drv")
        assert sp.is_derivation is True

    def test_is_derivation_drv_in_middle(self):
        """Only names ending with .drv are derivations."""
        sp = StorePath("a" * 32 + "-foo.drv.bar")
        assert sp.is_derivation is False

    def test_is_derivation_no_drv(self):
        sp = StorePath("a" * 32 + "-bash-5.2")
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


def test_derivation_from_dict():
    drv = Derivation.from_dict(
        {
            "name": "foo",
            "system": "x86_64-linux",
            "builder": "/bin/sh",
            "env": {"A": "1"},
            "input_srcs": ["/nix/store/src"],
            "input_drvs": {
                "/nix/store/input.drv": {
                    "outputs": ["out"],
                    "dynamic_outputs": {"dev": "out"},
                },
            },
        }
    )

    assert drv.system == "x86_64-linux"
    assert drv.env == {"A": "1"}
    assert drv.input_srcs == ["/nix/store/src"]
    assert drv.input_drvs["/nix/store/input.drv"].outputs == ["out"]
    assert drv.input_drvs["/nix/store/input.drv"].dynamic_outputs == {"dev": "out"}
