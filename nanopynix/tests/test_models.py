"""Tests for pydantic models — no Nix/C++ dependency."""

from __future__ import annotations

from nanopynix_proto.nix.common import AttrsValue

from nanopynix.models import (
    BuildResult,
    Derivation,
    DerivedPath,
    FlakeRef,
    Input,
    LockedFlake,
    LockedNode,
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
            },
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
            },
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
            },
        )
        assert inp.attrs["type"].string_value == "github"

    def test_indirect(self):
        inp = Input(
            attrs={
                "type": AttrsValue(string_value="indirect"),
                "id": AttrsValue(string_value="nixpkgs"),
            },
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
            },
        )
        assert fr.attrs["dir"].string_value == "lib"

    def test_basic(self):
        fr = FlakeRef(
            attrs={
                "type": AttrsValue(string_value="github"),
                "owner": AttrsValue(string_value="NixOS"),
                "repo": AttrsValue(string_value="nixpkgs"),
            },
        )
        assert fr.attrs["repo"].string_value == "nixpkgs"

    def test_default(self):
        fr = FlakeRef()
        assert fr.attrs == {}


# ── LockedNode / LockedFlake ─────────────────────────────────────────


class TestLockedNode:
    """One node of a lock graph, as ``LockFile::findInput`` returns it.

    This replaced a ``LockedInput`` message that carried the inputs a
    ``flake.nix`` *declares*, under a name that said locked. The graph now
    travels as Nix's own JSON, inside the metadata object.
    """

    def test_a_locked_node_carries_both_references(self):
        node = LockedNode(
            locked_ref="github:NixOS/nixpkgs/abc123",
            original_ref="github:NixOS/nixpkgs/nixos-24.11",
            is_flake=True,
        )
        assert node.locked_ref == "github:NixOS/nixpkgs/abc123"
        assert node.original_ref == "github:NixOS/nixpkgs/nixos-24.11"
        assert node.is_flake

    def test_a_node_that_is_not_a_flake(self):
        node = LockedNode(locked_ref="path:/x", original_ref="path:/x", is_flake=False)
        assert not node.is_flake

    def test_defaults(self):
        node = LockedNode()
        assert node.locked_ref == ""
        assert node.original_ref == ""
        assert node.is_flake is False  # proto default


class TestLockedFlake:
    def test_full(self):
        lf = LockedFlake(handle=1, description="A test flake")
        assert lf.handle == 1
        assert lf.description == "A test flake"

    def test_defaults(self):
        lf = LockedFlake()
        assert lf.handle == 0
        assert lf.description == ""


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

    def test_an_error_event_carries_nixs_structured_detail(self):
        """``logEI`` sends the dict ``NixError.info`` carries, beside the text.

        The payload here is the shape ``errinfo::to_dict`` builds. It is
        written out rather than taken from a live Nix, because what this
        asserts is that the transport keeps every level of it -- and a real
        warning does not populate every level. See the parity test in
        ``test_engine_parity_logging.py`` for the live half.
        """
        info = {
            "level": 1,
            "msg": "a warning",
            "pos": {"file": "«string»:1:1", "line": 1, "column": 1},
            "is_from_expr": True,
            "status": 1,
            "traces": [{"hint": "while evaluating x", "pos": {"file": "f.nix", "line": 2, "column": 3}}],
            "truncated": False,
            "suggestions": ["did you mean y?"],
        }
        ev = LogEvent(request_id=7, action="error", args=[1, "a warning", info])

        assert ev.error_info == info
        assert ev.error_info is not None
        assert ev.error_info["pos"] == {"file": "«string»:1:1", "line": 1, "column": 1}
        assert ev.error_info["traces"][0]["hint"] == "while evaluating x"
        assert ev.error_info["suggestions"] == ["did you mean y?"]

    def test_the_message_of_an_error_is_the_text_and_not_the_payload(self):
        """``args[-1]`` was the message until ``logEI`` put a dict after it."""
        ev = LogEvent(request_id=7, action="error", args=[1, "a warning", {"msg": "a warning"}])

        assert ev.message == "a warning"
        assert ev.message_without_ansi == "a warning"

    def test_only_an_error_action_has_a_payload(self):
        """``msg`` and ``warn`` reach Nix's logger as text, with no ErrorInfo."""
        assert LogEvent(request_id=1, action="msg", args=[3, "text"]).error_info is None
        assert LogEvent(request_id=1, action="warn", args=["text"]).error_info is None
        assert LogEvent(request_id=1, action="error", args=[1, "text"]).error_info is None

    def test_without_ansi_reaches_inside_the_payload(self):
        """A filtered event must not leave a coloured trace hint behind it.

        The strings a reader wants are one and two levels down -- the message,
        each trace hint, each suggestion -- so a filter that saw only the top
        level would answer one question two ways.
        """
        ev = LogEvent(
            request_id=7,
            action="error",
            args=[
                1,
                "\x1b[31ma warning\x1b[0m",
                {
                    "msg": "\x1b[31ma warning\x1b[0m",
                    "pos": {"file": "\x1b[35mf.nix\x1b[0m", "line": 1, "column": 1},
                    "traces": [{"hint": "\x1b[33mwhile evaluating x\x1b[0m", "pos": None}],
                    "suggestions": ["\x1b[32mdid you mean y?\x1b[0m"],
                    "is_from_expr": True,
                },
            ],
        )

        clean = ev.without_ansi().error_info

        assert clean is not None
        assert clean["msg"] == "a warning"
        assert clean["pos"] == {"file": "f.nix", "line": 1, "column": 1}
        assert clean["traces"][0]["hint"] == "while evaluating x"
        assert clean["suggestions"] == ["did you mean y?"]
        assert clean["is_from_expr"] is True, "a non-string must survive the filter unchanged"
        assert ev.error_info is not None
        assert "\x1b[" in ev.error_info["msg"], "the original event must keep what Nix sent"

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


# ── DerivedPath ──────────────────────────────────────────────────────

_DRV = "/nix/store/" + "a" * 32 + "-foo.drv"


class TestDerivedPath:
    def test_a_bare_path_has_no_selector(self):
        """``None``, not ``[]`` -- see the class docstring for why it matters."""
        dp = DerivedPath(_DRV)
        assert dp.drv_path == _DRV
        assert dp.outputs is None

    def test_the_all_selector_survives_as_nix_spells_it(self):
        dp = DerivedPath(_DRV + "^*")
        assert dp.drv_path == _DRV
        assert dp.outputs == ["*"]

    def test_named_outputs_are_kept_in_the_order_written(self):
        """Not sorted here. Nix canonicalises, and re-sorting would hide whether
        a round trip actually reached ``DerivedPath::parse``."""
        assert DerivedPath(_DRV + "^out,dev").outputs == ["out", "dev"]

    def test_drv_path_is_a_store_path(self):
        assert DerivedPath(_DRV + "^out").drv_path.is_derivation
        assert DerivedPath(_DRV + "^out").drv_path.name == "foo.drv"

    def test_the_split_takes_the_last_separator(self):
        """Mirrors Nix's ``parseWith``, which uses ``rfind('^')``. With dynamic
        derivations the head is itself derived and so is not a store path;
        reporting it verbatim is the only answer that does not lose it."""
        dp = DerivedPath(_DRV + "^out^bin")
        assert dp.drv_path == _DRV + "^out"
        assert dp.outputs == ["bin"]

    def test_no_selector_is_distinguishable_from_an_empty_one(self):
        """The whole reason :attr:`outputs` is optional rather than a list."""
        assert DerivedPath(_DRV).outputs is None
        assert DerivedPath(_DRV + "^").outputs == []

    def test_it_is_a_str_and_idempotent(self):
        dp = DerivedPath(_DRV + "^out")
        assert isinstance(dp, str)
        assert DerivedPath(dp) is dp

    def test_for_build_selects_every_output_of_a_bare_drv(self):
        """The one string the async stores rewrite before a binding sees it."""
        assert DerivedPath(_DRV).for_build() == _DRV + "^*"

    def test_for_build_leaves_a_written_selector_alone(self):
        """A caller that wrote ``^`` said what it wanted, including ``^*``."""
        for suffix in ("^out", "^out,dev", "^*", "^"):
            assert DerivedPath(_DRV + suffix).for_build() == _DRV + suffix

    def test_for_build_leaves_a_non_derivation_opaque(self):
        """A bare non-derivation path is a fetch, and that is a real request.

        Appending ``^*`` to it would ask Nix to build the outputs of something
        that is not a derivation, which is an error rather than a convenience.
        """
        plain = "/nix/store/00000000000000000000000000000000-foo"
        assert DerivedPath(plain).for_build() == plain

    def test_for_build_needs_no_store_directory(self):
        """It works on a relative path, which is why it can live in Python.

        Nix resolves a relative store path *before* it looks for the
        separator (``nix_store.cpp``'s ``parse_derived_paths``), so appending
        the selector first is correct and needs no store configuration. That
        is what makes this a string rule rather than a worker one.
        """
        assert DerivedPath("foo.drv").for_build() == "foo.drv^*"

    def test_for_build_is_idempotent(self):
        dp = DerivedPath(_DRV).for_build()
        assert dp.for_build() == dp
        assert isinstance(dp, DerivedPath)


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
    """``dynamic_outputs`` maps to a whole node, not an output name.

    This used to pass ``{"dev": "out"}``, matching a
    ``map<string, string>`` field. That shape could not represent Nix's
    ``DerivedPathMap``, which nests one level per level of dynamic derivation,
    so the binding truncated every child to its first output and dropped
    anything below it. The field is a recursive message now and the old
    expectation is unrepresentable rather than merely different.
    """
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
                    "dynamic_outputs": {"dev": {"outputs": ["out", "man"]}},
                },
            },
        },
    )

    assert drv.system == "x86_64-linux"
    assert drv.env == {"A": "1"}
    assert drv.input_srcs == ["/nix/store/src"]
    assert drv.input_drvs["/nix/store/input.drv"].outputs == ["out"]
    child = drv.input_drvs["/nix/store/input.drv"].dynamic_outputs["dev"]
    assert child.outputs == ["out", "man"], "a child with two outputs must keep both"
