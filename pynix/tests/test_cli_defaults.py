"""The four layers that give a repeated ``pynix`` option its default.

The flag, then the environment, then ``$XDG_CONFIG_HOME/pynix/config.toml``,
then the built-in default. One test for each boundary, and the tests that can
go through ``Pynix.parse`` do: ``ConfiguredCommand.__init__`` is what carries a
flag into the model, so a test that only built the model would pass while the
command line ignored the file.

``PYNIX_CONFIG`` points every test at its own ``tmp_path``. Without it a test
would read the configuration file of whoever runs the suite, and pass or fail
by that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nanopynix.settings import field_key
from pynix import parse
from pynix._impl.build import (
    _resolve_namespaced,  # pyright: ignore[reportPrivateUsage] -- the refusal under test is inside this function
)
from pynix._impl.settings import (
    DEFAULT_STORE,
    ConfigFileError,
    PynixDefaults,
    PynixNixSettings,
    configured_fields,
    nix_settings,
)
from pynix.build import Build

if TYPE_CHECKING:
    from pathlib import Path


def write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text)
    monkeypatch.setenv("PYNIX_CONFIG", str(path))
    return path


def no_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point at a file that does not exist, which is the ordinary case."""
    monkeypatch.setenv("PYNIX_CONFIG", str(tmp_path / "absent.toml"))


def build_command(*arguments: str) -> Build:
    """The `Build` that *arguments* names, resolved the way `main` resolves it.

    Narrowed to `Build`, because `parse` answers with whatever command the
    caller named and every test here names this one.
    """
    command = parse(["build", *arguments])
    assert isinstance(command, Build)
    return command


# ── one test for each boundary of the precedence order ───────────────


def test_the_built_in_default_applies_when_nothing_else_speaks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_config(tmp_path, monkeypatch)

    assert PynixDefaults().store == DEFAULT_STORE
    # `pynix` holds no Nix setting of its own. `nix build` holds none either,
    # and a value here would be sent to the worker and would replace what the
    # `nix.conf` of the host says -- which is issue #96.
    assert nix_settings().substituters is None
    assert nix_settings().trusted_public_keys is None


def test_the_file_beats_the_built_in_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(
        tmp_path,
        monkeypatch,
        '[defaults]\nstore = "daemon"\nverbosity = "notice"\nprint-build-logs = true\n'
        '\n[nix]\nsubstituters = ["https://file.example/"]\nmax-jobs = 8\n',
    )

    assert PynixDefaults().store == "daemon"
    assert PynixDefaults().verbosity == "notice"
    assert PynixDefaults().print_build_logs is True
    assert nix_settings().substituters == ["https://file.example/"]
    assert nix_settings().max_jobs == 8


def test_the_environment_beats_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(
        tmp_path,
        monkeypatch,
        '[defaults]\nstore = "daemon"\n\n[nix]\nsubstituters = ["https://file.example/"]\n',
    )
    monkeypatch.setenv("PYNIX_STORE", "local")
    monkeypatch.setenv("PYNIX_NIX_SUBSTITUTERS", "https://env.example/")

    assert PynixDefaults().store == "local"
    assert nix_settings().substituters == ["https://env.example/"]


def test_the_flag_beats_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Through the real parser, because clypi is what orders these two."""
    no_config(tmp_path, monkeypatch)
    monkeypatch.setenv("PYNIX_STORE", "local")

    command = build_command("--store", "daemon")

    assert command.store == "daemon"
    assert nix_settings(substituters="https://flag.example/").substituters == ["https://flag.example/"]


# ── the file reaches the parser, and not only the model ──────────────


def test_a_configured_store_becomes_the_default_of_a_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch, '[defaults]\nstore = "daemon"\n')

    command = parse(["path-info", "/nix/store/x"])

    assert command.store == "daemon"  # type: ignore[attr-defined] -- see test_the_flag_beats_the_environment


def test_a_configured_flag_that_is_true_can_still_be_turned_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason ``--print-build-logs`` grew a negative.

    A boolean whose default a file can set to true is unreachable from the
    command line without one.
    """
    write_config(tmp_path, monkeypatch, "[defaults]\nprint-build-logs = true\n")

    assert build_command().print_build_logs is True  # type: ignore[attr-defined] -- see above
    assert build_command("--no-print-build-logs").print_build_logs is False  # type: ignore[attr-defined] -- see above


def test_a_configured_store_does_not_refuse_a_namespaced_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--namespaced`` owns its store, so naming one is a contradiction.

    A configured default is not a request about this build, and must not be
    read as one. ``_resolve_namespaced`` asks ``explicit_options``, which holds only
    what the command line named.
    """
    write_config(tmp_path, monkeypatch, '[defaults]\nstore = "daemon"\n')

    command = build_command("--namespaced")

    assert command.store == "daemon"  # type: ignore[attr-defined] -- see above
    assert _resolve_namespaced(command) is True  # type: ignore[arg-type] -- build_command returns the parsed Build

    named = build_command("--namespaced", "--store", "daemon")
    with pytest.raises(SystemExit):
        _resolve_namespaced(named)  # type: ignore[arg-type] -- see above


# ── what the command line named ──────────────────────────────────────


def test_every_configured_option_holds_a_value_after_the_command_is_built(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ConfiguredCommand.__init__`` is what fills in a configured option.

    A command that reached ``run()`` without it would pass ``None`` to the
    library. clypi needed an ``UNSET`` sentinel to tell "absent" from
    "explicitly false"; argparse says it with ``SUPPRESS``, so the sentinel is
    gone and this asks the question the other way round. Issue #214.
    """
    no_config(tmp_path, monkeypatch)

    command = build_command()

    # Compared against the models, and not against ``None``: ``--eval-store``
    # and ``--verbosity`` resolve to ``None`` on purpose, so "is not None" would
    # be false for two options that are perfectly resolved.
    for model, fields in configured_fields(Build).items():
        resolved = model()
        for field in fields:
            assert getattr(command, field) == getattr(resolved, field), field


def test_explicit_holds_the_flags_and_nothing_else(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither the environment nor the file puts a name in ``explicit_options``."""
    write_config(tmp_path, monkeypatch, '[defaults]\nverbosity = "notice"\n')
    monkeypatch.setenv("PYNIX_STORE", "local")

    assert build_command().explicit_options == frozenset()
    assert build_command("--store", "daemon").explicit_options == frozenset({"store"})


def test_the_two_models_that_share_a_name_do_not_fight_over_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``store`` is a field of both models, and they mean different things.

    ``PynixDefaults.store`` is the store a command opens, and
    ``PynixNixSettings`` inherits Nix's global of the same name, which holds a
    store URL and defaults to ``None``. The second must not resolve the option.
    """
    no_config(tmp_path, monkeypatch)

    assert build_command().store == DEFAULT_STORE  # type: ignore[attr-defined] -- see above


# ── the regression that this work exists to fix ──────────────────────


def test_pynix_build_honours_the_substituters_of_the_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``pynix build`` passed its own clypi defaults into the settings model.

    ``pydantic-settings`` ranks the init source above the environment, so
    ``PYNIX_NIX_SUBSTITUTERS`` never took effect once, for any value.
    """
    no_config(tmp_path, monkeypatch)
    monkeypatch.setenv("PYNIX_NIX_SUBSTITUTERS", "https://env.example/")
    command = build_command()

    settings = nix_settings(
        substituters=command.substituters,  # type: ignore[attr-defined] -- see above
        trusted_public_keys=command.trusted_public_keys,  # type: ignore[attr-defined] -- see above
    )

    assert settings.substituters == ["https://env.example/"]


def test_the_nix_settings_keep_their_environment_prefix() -> None:
    """``PynixNixSettings`` inherits two settings models, and pydantic merges
    ``model_config`` over the bases. Without the restatement the mixin reset
    ``env_prefix`` to "", so every ``PYNIX_NIX_*`` variable stopped being read
    and the environment lost to the file with no message.
    """
    assert PynixNixSettings.model_config.get("env_prefix") == "PYNIX_NIX_"
    assert PynixDefaults.model_config.get("env_prefix") == "PYNIX_"


@pytest.mark.parametrize(
    ("variable", "expected"),
    [("PYNIX_NIX_CORES", 9), ("cores", None)],
)
def test_only_the_prefixed_spelling_reaches_a_nix_setting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    expected: int | None,
) -> None:
    """``PYNIX_NIX_`` is the documented prefix, and the only one that works.

    Every field carries its ``nix.conf`` key as an alias, and
    pydantic-settings reads an alias as a second environment name with no
    prefix in front of it. The unprefixed name also won, because
    ``_extract_field_info`` builds the alias entry before the prefixed one.
    """
    no_config(tmp_path, monkeypatch)
    monkeypatch.setenv(variable, "9")

    assert PynixNixSettings().cores == expected


def test_a_common_word_in_the_environment_changes_no_setting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every single-word ``nix.conf`` key, set at once, and none is read.

    This list is what makes a new one-word setting safe: a field added to a
    scope model joins it, and this test then covers that field too. ``stdenv``
    exports ``system``, so the shell of Nix that runs this suite already sets
    one of these names.
    """
    no_config(tmp_path, monkeypatch)
    single_word = sorted(
        key for name, field in PynixNixSettings.model_fields.items() if "-" not in (key := field_key(name, field))
    )
    for key in single_word:
        monkeypatch.setenv(key, "0")

    named = PynixNixSettings().model_fields_set

    assert single_word, "no single-word key found; the scan is broken, not the model"
    assert named == set(), f"these settings were read from an unprefixed name: {sorted(named)}"


def test_the_defaults_model_also_refuses_an_unprefixed_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``store`` is a common word, and it names which store a command opens."""
    no_config(tmp_path, monkeypatch)
    monkeypatch.setenv("store", "/tmp/not-the-store")

    assert PynixDefaults().store == DEFAULT_STORE


def test_every_table_backed_model_still_reads_its_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stricter environment source must not cost the configuration file.

    ``PynixNixSettings`` inherits ``NixSettingsEnv`` first, and that base
    states a source order of its own which names no file. Inheriting it
    dropped the ``[nix]`` table, so this reads one key from each table rather
    than trusting the method resolution order.

    Two other tests here fail on the same cause, and both report a value that
    is wrong rather than the reason it is wrong. This one names the reason.
    """
    write_config(
        tmp_path,
        monkeypatch,
        '[defaults]\nstore = "daemon"\n\n[nix]\nmax-jobs = 8\n',
    )

    assert PynixDefaults().store == "daemon"
    assert PynixNixSettings().max_jobs == 8


# ── what the file may say, and what it may not ───────────────────────


def test_a_missing_configuration_file_is_not_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    no_config(tmp_path, monkeypatch)

    assert PynixDefaults().store == DEFAULT_STORE


def test_a_file_with_neither_table_is_not_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, '[something-else]\nkey = "value"\n')

    assert PynixDefaults().store == DEFAULT_STORE


def test_a_file_that_does_not_parse_names_itself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_config(tmp_path, monkeypatch, "[defaults\nstore =\n")

    with pytest.raises(ConfigFileError, match=str(path)):
        PynixDefaults()


def test_a_table_that_is_not_a_table_names_itself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, 'defaults = "daemon"\n')

    with pytest.raises(ConfigFileError, match=r"\[defaults\]"):
        PynixDefaults()


def test_an_unknown_key_is_reported_and_not_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``extra="forbid"``. A typo that the file ignores is a setting a user
    believes is applied."""
    write_config(tmp_path, monkeypatch, '[defaults]\nstor = "daemon"\n')

    with pytest.raises(ValueError, match="stor"):
        PynixDefaults()


def test_the_file_takes_the_nix_conf_spelling_of_a_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A TOML array is the natural spelling here, and the other one still works."""
    write_config(tmp_path, monkeypatch, '[nix]\nsubstituters = "https://a.example/ https://b.example/"\n')

    assert nix_settings().substituters == ["https://a.example/", "https://b.example/"]
