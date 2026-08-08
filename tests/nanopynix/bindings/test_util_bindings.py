"""Tests for Nix C++ utility functions bound in nanopynix_util and nanopynix_store."""

from __future__ import annotations

from typing import Any

import pytest
from nanopynix_bindings import store as nanopynix_store, util as nanopynix_util

import nanopynix


def test_build_info_reports_compile_time_compatibility() -> None:
    info: Any = nanopynix_util.build_info()  # type: ignore[reportUnknownVariableType, reportUnknownMemberType] -- C++ extension without type stubs

    assert isinstance(info["nix_version"], str)
    assert info["nix_version"]
    assert set(info["capabilities"]) == {  # type: ignore[reportUnknownArgumentType] -- build_info keys are dynamic
        "logger_unique_ptr",
        "build_result_sum",
        "eval_state_mem",
        "dynamic_primop_registration",
        "store_impl_read_derivation",
        # `Store.submit_output`, the one operation of `builder-rpc-v0`. False
        # on every Nix release, because the feature reaches none of them:
        # NixOS/nix#15793 merged after 2.35, so only `nix/nix-master.nix`
        # builds a Nix that has it.
        "store_submit_output",
        # The one capability here that the Nix version does not decide. It
        # comes from `-Dgc=disabled`, which the AddressSanitizer variant
        # builds because libexpr refuses ASAN together with a conservative
        # collector. So this key is true on four of the five variants that
        # `default.nix` builds, and the Nix version says nothing about it.
        "boehm_gc",
    }
    assert all(isinstance(value, bool) for value in info["capabilities"].values())


def test_build_info_is_available_from_nanopynix() -> None:
    assert nanopynix.build_info() == nanopynix_util.build_info()  # type: ignore[reportUnknownVariableType] -- build_info from C++ extension


def test_unsupported_primop_registration_fails_early() -> None:
    if nanopynix.build_info()["capabilities"]["dynamic_primop_registration"]:  # type: ignore[reportUnknownMemberType, reportUnknownVariableType] -- build_info from C++ extension
        pytest.skip("linked Nix supports dynamic primop registration")

    with pytest.raises(RuntimeError, match="fixed-capacity builtin attribute set"):
        nanopynix.register_primop("unsupported", 1, ["value"], "", lambda value: value)  # type: ignore[reportUnknownLambdaType, reportUnknownArgumentType] -- primop callbacks receive Any from Nix


class TestCurrentSystem:
    def test_current_system_is_non_empty(self):
        assert nanopynix_util.current_system()

    def test_current_system_uses_system_setting(self):
        previous_system = nanopynix_util.get_setting("system")
        previous_eval_system = nanopynix_util.get_setting("eval-system")
        if previous_eval_system is not None:
            nanopynix_util.set_setting("eval-system", "")
        nanopynix_util.set_setting("system", "mips64-linux")
        try:
            assert nanopynix_util.current_system() == "mips64-linux"
        finally:
            nanopynix_util.set_setting("system", previous_system or "")
            if previous_eval_system is not None:
                nanopynix_util.set_setting("eval-system", previous_eval_system)


class TestFilterAnsiEscapes:
    """``nix::filterANSIEscapes``, which replaced the ``strip-ansi`` package.

    Each case below is a shape that the pattern in that package left behind.
    ``nanopynix/_ansi.py`` gives the measurement and the reason.
    """

    def test_strips_the_colours_that_nix_writes(self):
        assert nanopynix_util.filter_ansi_escapes("\x1b[31;1merror\x1b[0m", filter_all=True) == "error"

    def test_strips_an_osc_8_hyperlink(self):
        # Both terminators, because gcc writes BEL and the standard writes
        # ESC backslash. Nix tests both as well.
        assert nanopynix_util.filter_ansi_escapes("\x1b]8;;http://x\x1b\\a\x1b]8;;\x1b\\", filter_all=True) == "a"
        assert nanopynix_util.filter_ansi_escapes("\x1b]8;;http://x\aa\x1b]8;;\a", filter_all=True) == "a"

    def test_strips_a_24_bit_colour(self):
        # Five parameters. A pattern that allows three leaves the opening
        # sequence in the result.
        assert nanopynix_util.filter_ansi_escapes("\x1b[38;2;255;0;0mred\x1b[0m", filter_all=True) == "red"

    def test_strips_a_sequence_that_does_not_end_in_m(self):
        assert nanopynix_util.filter_ansi_escapes("\x1b[2Kgone", filter_all=True) == "gone"

    def test_keeps_a_colour_when_filter_all_is_false(self):
        # The default, and the reason `strip_ansi` passes True. A colour
        # sequence stays and counts for no width.
        text = "\x1b[31;1merror\x1b[0m"
        assert nanopynix_util.filter_ansi_escapes(text) == text

    def test_expands_a_tab_and_drops_a_carriage_return_and_a_bell(self):
        # Nix's behaviour, and a difference from the package this replaced.
        # `nanopynix.strip_ansi` states each one, because a caller that
        # compares an exact string meets all three.
        assert nanopynix_util.filter_ansi_escapes("a\tb\rc", filter_all=True) == "a       bc"
        assert nanopynix_util.filter_ansi_escapes("a\ab", filter_all=True) == "ab"

    def test_keeps_the_other_control_characters(self):
        # The boundary of the rule above. Nix drops the carriage return and
        # the bell only, so a backspace and a vertical tab reach the caller.
        assert nanopynix_util.filter_ansi_escapes("a\x08b\x0bc", filter_all=True) == "a\x08b\x0bc"

    def test_width_truncates_and_none_does_not(self):
        assert nanopynix_util.filter_ansi_escapes("abcdef", filter_all=True, width=3) == "abc"
        assert nanopynix_util.filter_ansi_escapes("abcdef", filter_all=True, width=None) == "abcdef"
        assert nanopynix_util.filter_ansi_escapes("abcdef", filter_all=True, width=0) == ""

    def test_a_negative_width_is_refused(self):
        # Nix takes an unsigned int, and -1 as an unsigned int is "do not
        # truncate" -- the opposite of what the caller asked for. Pinned here
        # because only the parameter type refuses it.
        with pytest.raises(TypeError):
            nanopynix_util.filter_ansi_escapes("abcdef", filter_all=True, width=-1)

    def test_needs_no_initialisation(self):
        """The function reads no configuration, so it runs before any init.

        This asserts the property that lets `nanopynix._ansi.strip_ansi` sit
        in a pydantic validator and in an exception accessor, which both run
        wherever the caller happens to be. It cannot assert that no init has
        happened in this process, because another test may have run first.
        """
        assert "filter_ansi_escapes" in dir(nanopynix_util)
        assert nanopynix_util.filter_ansi_escapes("plain") == "plain"


class TestUrlUtilities:
    def test_percent_encode_default(self):
        assert nanopynix_util.percent_encode("hello world") == "hello%20world"

    def test_percent_encode_keep(self):
        assert nanopynix_util.percent_encode("hello world", keep="/") == "hello%20world"
        assert nanopynix_util.percent_encode("a/b c", keep="/") == "a/b%20c"

    def test_percent_decode(self):
        assert nanopynix_util.percent_decode("hello%20world") == "hello world"
        assert nanopynix_util.percent_decode("%2Fetc%2Fhosts") == "/etc/hosts"

    def test_fix_git_url_scp(self):
        url = nanopynix_util.fix_git_url("git@github.com:org/repo.git")
        assert url.startswith("ssh://git@github.com/org/repo")

    def test_fix_git_url_git_plus(self):
        url = nanopynix_util.fix_git_url("git+https://github.com/org/repo")
        assert url == "https://github.com/org/repo"

    def test_fix_git_url_already_ok(self):
        url = nanopynix_util.fix_git_url("https://github.com/org/repo")
        assert url == "https://github.com/org/repo"

    def test_is_valid_scheme_name(self):
        assert nanopynix_util.is_valid_scheme_name("https") is True
        assert nanopynix_util.is_valid_scheme_name("file") is True
        assert nanopynix_util.is_valid_scheme_name("git+ssh") is True
        assert nanopynix_util.is_valid_scheme_name("9bad") is False
        assert nanopynix_util.is_valid_scheme_name("has:colon") is False


class TestHashUtilities:
    def test_parse_hash_algo(self):
        assert nanopynix_util.parse_hash_algo("sha256") == 44
        assert nanopynix_util.parse_hash_algo("sha512") == 45
        assert nanopynix_util.parse_hash_algo("sha1") == 43
        assert nanopynix_util.parse_hash_algo("md5") == 42

    def test_parse_hash_algo_opt(self):
        assert nanopynix_util.parse_hash_algo_opt("sha256") == 44
        assert nanopynix_util.parse_hash_algo_opt("bogus") is None
        assert nanopynix_util.parse_hash_algo_opt("") is None

    def test_parse_hash_algo_invalid(self):
        with pytest.raises(RuntimeError, match="hash algorithm"):
            nanopynix_util.parse_hash_algo("not_a_hash")

    def test_print_hash_algo(self):
        assert nanopynix_util.print_hash_algo(44) == "sha256"
        assert nanopynix_util.print_hash_algo(42) == "md5"
        assert nanopynix_util.print_hash_algo(46) == "blake3"

    def test_parse_hash_format(self):
        assert nanopynix_util.parse_hash_format("base64") == 0
        assert nanopynix_util.parse_hash_format("nix32") == 1
        assert nanopynix_util.parse_hash_format("base16") == 2

    def test_parse_hash_format_invalid(self):
        with pytest.raises(RuntimeError, match="hash format"):
            nanopynix_util.parse_hash_format("bogus")

    def test_print_hash_format(self):
        assert nanopynix_util.print_hash_format(0) == "base64"
        assert nanopynix_util.print_hash_format(1) == "nix32"
        assert nanopynix_util.print_hash_format(2) == "base16"

    def test_parse_hash_any_sri(self):
        result = nanopynix_util.parse_hash_any("sha256-47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=")
        assert result == "sha256-47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU="

    def test_parse_hash_any_hex(self):
        result = nanopynix_util.parse_hash_any(
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            algo=44,
        )
        assert result == "sha256-47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU="

    def test_parse_hash_any_with_algo(self):
        result = nanopynix_util.parse_hash_any(
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            algo=44,
        )
        assert result == "sha256-47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU="


class TestStoreUtilities:
    def test_compare_versions_greater(self):
        assert nanopynix_store.compare_versions("2.0", "1.0") == 1
        assert nanopynix_store.compare_versions("2.0", "2.0-pre") == 1

    def test_compare_versions_less(self):
        assert nanopynix_store.compare_versions("1.0", "2.0") == -1
        assert nanopynix_store.compare_versions("1.0-pre", "1.0") == -1

    def test_compare_versions_equal(self):
        assert nanopynix_store.compare_versions("1.0", "1.0") == 0

    def test_compare_versions_numeric_segments(self):
        assert nanopynix_store.compare_versions("2.10", "2.2") == 1

    def test_check_name_valid(self):
        assert nanopynix_store.check_name("hello") is True
        assert nanopynix_store.check_name("hello-world-1.0") is True
        assert nanopynix_store.check_name("x") is True

    def test_check_name_dot(self):
        with pytest.raises(RuntimeError, match="not valid"):
            nanopynix_store.check_name(".")

    def test_check_name_empty(self):
        with pytest.raises(RuntimeError, match="must not be empty"):
            nanopynix_store.check_name("")


class TestParseStoreReference:
    def test_auto(self):
        result: Any = nanopynix_store.parse_store_reference("auto")  # type: ignore[reportUnknownVariableType, reportUnknownMemberType] -- C++ extension without type stubs
        assert result["type"] == "Auto"
        assert result["scheme"] is None
        assert result["authority"] is None
        assert result["params"] == {}
        assert result["render"] == "auto"

    def test_daemon_shorthand(self):
        result: Any = nanopynix_store.parse_store_reference("daemon")  # type: ignore[reportUnknownVariableType, reportUnknownMemberType] -- C++ extension without type stubs
        assert result["type"] == "Daemon"
        assert result["render"] == "daemon"
        assert result["render_without_params"] == "daemon"

    def test_unix_socket_is_not_collapsed_to_daemon(self):
        """Parsing a raw string never applies the "daemon" shorthand.

        That collapsing is a property of an *open* store's ``getReference()``,
        which compares its resolved socket path against Nix's live default
        (``settings.nixDaemonSocketFile``) -- see ``Store.get_uri()``. Parsing
        a "unix://" string in isolation has no store to compare against, so it
        always stays "Specified", even if the path happens to match the
        default daemon socket.
        """
        result: Any = nanopynix_store.parse_store_reference(  # type: ignore[reportUnknownVariableType, reportUnknownMemberType] -- C++ extension without type stubs
            "unix:///tmp/foo/socket?root=/tmp/foo",
        )
        assert result["type"] == "Specified"
        assert result["scheme"] == "unix"
        assert result["authority"] == "/tmp/foo/socket"
        assert result["params"] == {"root": "/tmp/foo"}
        assert result["render"] == "unix:///tmp/foo/socket?root=/tmp/foo"
        assert result["render_without_params"] == "unix:///tmp/foo/socket"

    def test_local_with_params(self):
        result: Any = nanopynix_store.parse_store_reference("local?root=/tmp/x")  # type: ignore[reportUnknownVariableType, reportUnknownMemberType] -- C++ extension without type stubs
        assert result["type"] == "Specified"
        assert result["scheme"] == "local"
        assert result["params"] == {"root": "/tmp/x"}

    def test_bare_path(self):
        result: Any = nanopynix_store.parse_store_reference("/tmp/some/path")  # type: ignore[reportUnknownVariableType, reportUnknownMemberType] -- C++ extension without type stubs
        assert result["type"] == "Specified"
        assert result["scheme"] == "local"
        assert result["authority"] == "/tmp/some/path"


class TestRenderStoreReference:
    def test_round_trips_daemon(self):
        assert nanopynix_store.render_store_reference("daemon") == "daemon"

    def test_round_trips_unix_socket_with_params(self):
        uri = "unix:///tmp/foo/socket?root=/tmp/foo"
        assert nanopynix_store.render_store_reference(uri) == uri

    def test_without_params_drops_query_string(self):
        uri = "unix:///tmp/foo/socket?root=/tmp/foo"
        assert nanopynix_store.render_store_reference(uri, with_params=False) == "unix:///tmp/foo/socket"

    def test_round_trips_auto(self):
        assert nanopynix_store.render_store_reference("auto") == "auto"
