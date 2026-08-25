from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import pytest
from anyio import Path as AnyioPath
from nanopynix_helpers import build as nanopynix_helpers_build
from nanopynix_helpers.fod import replace_fod_hash as _real_replace_fod_hash

import nanopynix
from nanopynix._ansi import strip_ansi
from nanopynix.exceptions import EvalError, StoreError
from nanopynix_testing.nix_environment import with_nixpkgs
from nanopynix_testing.nix_markers import LINUX_CHROOT_BUILD
from pynix import parse
from pynix._impl.build import (  # pyright: ignore[reportPrivateUsage] -- test drives the private build step directly to exercise FOD-update/dry-run paths
    BuildTargetError,
    _build_target,
)
from pynix.target import EvaluationTarget
from test_support.git_fixtures import init_flake_repo

if TYPE_CHECKING:
    from pathlib import Path

    from nanopynix.rpc import Store, ValueProxy
    from nanopynix_testing.nix_environment import NixTestEnvironment


async def _fixed_output_derivations_in_value(
    value: ValueProxy,
    visited: set[int],
    derivations: set[str],
) -> None:
    """Walk values exposed from an evaluated root without resolving strings."""
    value_type = await value.get_type()
    if value.handle in visited:
        return
    visited.add(value.handle)
    if value_type == nanopynix.NixType.LIST:
        for index in range(await value.list_length()):
            await _fixed_output_derivations_in_value(value.list_get(index), visited, derivations)
        return
    if value_type != nanopynix.NixType.ATTRS:
        return
    if await value.has_attr("type") and await value.attr("type").to_python() == "derivation":
        drv_attrs = value.attr("drvAttrs")
        if await drv_attrs.has_attr("outputHash"):
            drv_path = await value.attr("drvPath").to_python()
            if not isinstance(drv_path, str):
                raise TypeError("derivation drvPath was not a string")
            derivations.add(drv_path)
        # A derivation attrset contains Nixpkgs convenience fields such as
        # ``all`` that can be recursively self-referential. Dependencies are
        # carried by its serialized inputDrvs, not those fields.
        return
    for name in await value.attr_names():
        await _fixed_output_derivations_in_value(value.attr(name), visited, derivations)


async def _fixed_output_derivations_in_closure(store: Store, root_drv_path: str) -> set[str]:
    """Walk serialized input derivations without building any derivation."""
    pending = [root_drv_path]
    visited: set[str] = set()
    fixed_output: set[str] = set()
    while pending:
        drv_path = pending.pop()
        if drv_path in visited:
            continue
        visited.add(drv_path)
        derivation = await store.read_derivation(drv_path)
        if any(output.type == "CAFixed" for output in derivation.outputs.values()):
            fixed_output.add(drv_path)
        pending.extend(derivation.input_drvs)
    return fixed_output


async def test_build_file_derivation(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "build-test.nix"
    nix_file.write_text(
        with_nixpkgs(
            """
    let
      pkgs = import <nixpkgs> {};
    in
      pkgs.stdenvNoCC.mkDerivation {
        pname = "pynix-build-file-test";
        version = "1";
        dontUnpack = true;
        installPhase = ''
          echo built-from-file > "$out"
        '';
      }
    """,
            nixpkgs_path,
        )
    )
    cmd = parse(["build", "--file", str(nix_file), *shared_nix_environment.pynix_store_args()])

    await cmd.run()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    out_path = data["outputs"]["out"]
    assert "pynix-build-file-test" in out_path
    assert await AnyioPath(shared_nix_environment.physical_path(out_path)).read_text() == "built-from-file\n"


async def test_build_file_derivation_attr(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "build-test.nix"
    nix_file.write_text(
        with_nixpkgs(
            """
    let
      pkgs = import <nixpkgs> {};
    in
    {
      package = pkgs.stdenvNoCC.mkDerivation {
        pname = "pynix-build-attr-test";
        version = "1";
        dontUnpack = true;
        installPhase = ''
          echo built-from-attr > "$out"
        '';
      };
    }
    """,
            nixpkgs_path,
        )
    )
    cmd = parse(
        [
            "build",
            "--file",
            str(nix_file),
            "--attr",
            "package",
            *shared_nix_environment.pynix_store_args(),
            "--print-build-logs",
        ],
    )

    await cmd.run()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    out_path = data["outputs"]["out"]
    assert "pynix-build-attr-test" in out_path
    assert await AnyioPath(shared_nix_environment.physical_path(out_path)).read_text() == "built-from-attr\n"


async def test_build_file_auto_calls_defaulted_lambda_before_attr(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "default.nix"
    nix_file.write_text(
        with_nixpkgs(
            """
    { pkgs ? import <nixpkgs> {}, name ? "pynix-build-autocall-test" }:
    {
      package = pkgs.stdenvNoCC.mkDerivation {
        pname = name;
        version = "1";
        dontUnpack = true;
        installPhase = ''
          echo built-from-autocall > "$out"
        '';
      };
    }
    """,
            nixpkgs_path,
        )
    )
    cmd = parse(
        ["build", "--file", str(nix_file), "--attr", "package", *shared_nix_environment.pynix_store_args()],
    )

    await cmd.run()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    out_path = data["outputs"]["out"]
    assert "pynix-build-autocall-test" in out_path
    assert await AnyioPath(shared_nix_environment.physical_path(out_path)).read_text() == "built-from-autocall\n"


async def test_build_missing_attr_errors_before_build(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "build-test.nix"
    nix_file.write_text(
        with_nixpkgs(
            """
    let
      pkgs = import <nixpkgs> {};
    in
    {
      package = pkgs.stdenvNoCC.mkDerivation {
        pname = "pynix-build-attr-test";
        version = "1";
        dontUnpack = true;
        installPhase = ''
          echo should-not-build > "$out"
        '';
      };
    }
    """,
            nixpkgs_path,
        )
    )
    cmd = parse(
        ["build", "--file", str(nix_file), "--attr", "missing", *shared_nix_environment.pynix_store_args()],
    )

    with pytest.raises(SystemExit):
        await cmd.run()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "attribute 'missing' not found" in strip_ansi(captured.err)


async def test_build_names_the_toplevel_of_a_nixos_configuration(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nix answers `selected value is not a derivation`, which is the end of the road.

    That message says neither what the value is nor which path would work. The
    module system stamps `_type` and `class` on the result of `nixosSystem`, so
    two attribute reads answer both.
    """
    nix_file = tmp_path / "configuration.nix"
    nix_file.write_text(
        """
    {
      hetztop = {
        _type = "configuration";
        class = "nixos";
        config = { system.build.toplevel = "unused"; };
        options = { };
      };
    }
    """
    )
    cmd = parse(["build", "--file", str(nix_file), "--attr", "hetztop", *shared_nix_environment.pynix_store_args()])

    with pytest.raises(SystemExit):
        await cmd.run()

    error = strip_ansi(capsys.readouterr().err)
    assert "hetztop is a NixOS configuration, not a derivation" in error
    assert "hetztop.config.system.build.toplevel" in error


async def test_build_names_the_activation_package_of_a_home_manager_configuration(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """home-manager sets `class = "homeManager"`, and holds its derivation elsewhere."""
    nix_file = tmp_path / "home.nix"
    nix_file.write_text(
        """
    {
      me = {
        _type = "configuration";
        class = "homeManager";
        config = { home.activationPackage = "unused"; };
        options = { };
      };
    }
    """
    )
    cmd = parse(["build", "--file", str(nix_file), "--attr", "me", *shared_nix_environment.pynix_store_args()])

    with pytest.raises(SystemExit):
        await cmd.run()

    error = strip_ansi(capsys.readouterr().err)
    assert "me is a home-manager configuration, not a derivation" in error
    assert "me.config.home.activationPackage" in error


@pytest.mark.parametrize("attribute", ["unknown_class", "plain_set"])
async def test_build_keeps_the_message_of_nix_for_any_other_value(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    attribute: str,
) -> None:
    """A `class` that is not one of the two named gets no guess.

    A set that holds `config` and `options` is any evaluation of the module
    system, and there are more of those than the two that this knows.
    """
    nix_file = tmp_path / "other.nix"
    nix_file.write_text(
        """
    {
      unknown_class = {
        _type = "configuration";
        class = "someOtherModuleSystem";
        config = { };
        options = { };
      };
      plain_set = { a = 1; };
    }
    """
    )
    cmd = parse(["build", "--file", str(nix_file), "--attr", attribute, *shared_nix_environment.pynix_store_args()])

    # The message of Nix, and no hint added to it. `run()` is called directly
    # here, so the error arrives as itself; `pynix._impl.main.run` is what turns
    # it into one line for a user.
    with pytest.raises(EvalError, match="selected value is not a derivation"):
        await cmd.run()


async def test_build_flake_derivation(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    git_flake: Path,
) -> None:
    cmd = parse(["build", "--flake", f"{git_flake}#hello", *shared_nix_environment.pynix_store_args()])

    await cmd.run()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    out_path = data["outputs"]["out"]
    assert "test-hello" in out_path
    assert await AnyioPath(shared_nix_environment.physical_path(out_path)).read_text() == "hi\n"


async def test_build_missing_input_errors(capsys: pytest.CaptureFixture[str]) -> None:
    cmd = parse(["build"])

    with pytest.raises(SystemExit):
        await cmd.run()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "either --file or --flake is required" in captured.err


async def test_build_update_fod_without_file_errors(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    git_flake: Path,
) -> None:
    cmd = parse(
        ["build", "--flake", f"{git_flake}#hello", "--update-fod", *shared_nix_environment.pynix_store_args()],
    )

    with pytest.raises(SystemExit):
        await cmd.run()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--update-fod requires --file to name a local file" in captured.err


async def test_build_update_fod_with_a_fetched_file_errors(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--file now accepts a reference, and --update-fod still needs a local file.

    ``<nixpkgs>`` resolves through the lookup path into the store, which is
    read-only. The guard refuses it before any evaluation starts, so this test
    needs no network and no lookup path that exists.
    """
    cmd = parse(
        ["build", "--file", "<nixpkgs>", "--update-fod", *shared_nix_environment.pynix_store_args()],
    )

    with pytest.raises(SystemExit):
        await cmd.run()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--update-fod requires --file to name a local file" in captured.err


async def test_build_dry_run_without_update_fod_errors(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "build-test.nix"
    nix_file.write_text("1\n")
    cmd = parse(["build", "--file", str(nix_file), "--dry-run", *shared_nix_environment.pynix_store_args()])

    with pytest.raises(SystemExit):
        await cmd.run()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--dry-run requires --update-fod" in captured.err


@LINUX_CHROOT_BUILD
async def test_build_propagates_a_non_fod_build_failure(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A build failure unrelated to a FOD hash mismatch must surface as the
    original StoreError, not be swallowed or turned into a FOD update."""
    nix_file = tmp_path / "build-test.nix"
    nix_file.write_text(
        with_nixpkgs(
            """
    let
      pkgs = import <nixpkgs> {};
    in
      pkgs.stdenvNoCC.mkDerivation {
        pname = "pynix-build-fails-test";
        version = "1";
        dontUnpack = true;
        installPhase = "exit 1";
      }
    """,
            nixpkgs_path,
        )
    )
    cmd = parse(["build", "--file", str(nix_file), *shared_nix_environment.pynix_store_args()])

    with pytest.raises(StoreError):
        await cmd.run()

    assert capsys.readouterr().out == ""


async def test_build_with_separate_eval_store(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "build-test.nix"
    nix_file.write_text(
        with_nixpkgs(
            """
    let
      pkgs = import <nixpkgs> {};
    in
      pkgs.stdenvNoCC.mkDerivation {
        pname = "pynix-build-eval-store-test";
        version = "1";
        dontUnpack = true;
        installPhase = ''
          echo built-with-eval-store > "$out"
        '';
      }
    """,
            nixpkgs_path,
        )
    )
    cmd = parse(
        [
            "build",
            "--file",
            str(nix_file),
            *shared_nix_environment.pynix_store_args(),
            "--eval-store",
            shared_nix_environment.store_uri,
        ],
    )

    await cmd.run()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    out_path = data["outputs"]["out"]
    assert "pynix-build-eval-store-test" in out_path
    assert await AnyioPath(shared_nix_environment.physical_path(out_path)).read_text() == "built-with-eval-store\n"


async def test_build_update_fod_dry_run_reports_local_fetchurl_diff(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = tmp_path / "payload"
    payload.write_text("fixed-output payload\n")
    nix_file = tmp_path / "source.nix"
    source = f'builtins.fetchurl {{ url = "file://{payload}"; sha256 = ""; }}\n'
    nix_file.write_text(source)
    cmd = parse(
        ["build", "--file", str(nix_file), "--update-fod", "--dry-run", *shared_nix_environment.pynix_store_args()],
    )

    await cmd.run()

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"dryRun": True, "outputs": {}, "updatedFods": 1}
    assert "-builtins.fetchurl" in strip_ansi(captured.err)
    assert "+builtins.fetchurl" in strip_ansi(captured.err)
    assert nix_file.read_text() == source


async def test_build_update_fod_dry_run_updates_a_lib_fakehash_binding(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`lib.fakeHash` is how nixpkgs says "I have not computed this hash".

    The updater read a plain string and nothing else, so the convention an
    author actually writes reported that the file holds no hash literal.
    Issue #109.
    """
    payload = tmp_path / "payload"
    payload.write_text("fixed-output payload\n")
    nix_file = tmp_path / "source.nix"
    source = with_nixpkgs(
        f'with import <nixpkgs> {{}};\nbuiltins.fetchurl {{ url = "file://{payload}"; sha256 = lib.fakeHash; }}\n',
        nixpkgs_path,
    )
    nix_file.write_text(source)
    cmd = parse(
        ["build", "--file", str(nix_file), "--update-fod", "--dry-run", *shared_nix_environment.pynix_store_args()],
    )

    await cmd.run()

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"dryRun": True, "outputs": {}, "updatedFods": 1}
    # The diff is wrapped for a terminal, and the wrap point moves with the
    # length of the temporary path, so it lands inside `sha256 = "sha256:...`
    # under one root and not under another. Match the text with every run of
    # whitespace removed: the rendering then cannot decide whether the
    # assertion holds.
    flattened = re.sub(r"\s+", "", strip_ansi(captured.err))
    # The symbol leaves and a quoted hash takes its place. Nix reports the
    # computed hash in base32 here and in SRI elsewhere, so the assertion
    # names the attribute and the algorithm and not the encoding.
    assert "lib.fakeHash" in flattened
    assert re.search(r'sha256="sha256[:-][A-Za-z0-9+/=]+"', flattened)
    assert nix_file.read_text() == source


@LINUX_CHROOT_BUILD
async def test_build_update_fod_rewrites_a_lib_fakehash_binding_and_rebuilds(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The symbol leaves the file, a quoted hash takes its place, and the build runs.

    `builtins.fetchurl` cannot carry this case: it yields a path rather than a
    derivation, so the rebuild after the rewrite has nothing to build. Issue #109.
    """
    nix_file = tmp_path / "source.nix"
    nix_file.write_text(
        with_nixpkgs(
            """with import <nixpkgs> {};
runCommand "payload" {
  outputHash = lib.fakeHash;
  outputHashAlgo = "sha256";
  outputHashMode = "flat";
} "printf '%s\\n' fixed-output-payload > $out"
""",
            nixpkgs_path,
        ),
    )
    cmd = parse(["build", "--file", str(nix_file), "--update-fod", *shared_nix_environment.pynix_store_args()])

    await cmd.run()

    data = json.loads(capsys.readouterr().out)
    assert data["updatedFods"] == 1
    assert data["outputs"]["out"]
    updated = nix_file.read_text()
    assert "lib.fakeHash" not in updated
    assert re.search(r'outputHash = "sha256[:-][A-Za-z0-9+/=]+"', updated)


@LINUX_CHROOT_BUILD
async def test_build_update_fod_rewrites_and_rebuilds(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "source.nix"
    nix_file.write_text(
        with_nixpkgs(
            """with import <nixpkgs> {};
runCommand "payload" {
  outputHash = "";
  outputHashAlgo = "sha256";
  outputHashMode = "flat";
} "printf '%s\\n' fixed-output-payload > $out"
""",
            nixpkgs_path,
        ),
    )
    cmd = parse(["build", "--file", str(nix_file), "--update-fod", *shared_nix_environment.pynix_store_args()])

    await cmd.run()

    data = json.loads(capsys.readouterr().out)
    assert data["updatedFods"] == 1
    assert data["outputs"]["out"]
    assert 'outputHash = "sha256-' in nix_file.read_text()


@LINUX_CHROOT_BUILD
async def test_build_update_fod_rewrites_each_named_run_command_dependency(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "source.nix"
    nix_file.write_text(
        with_nixpkgs(
            """with import <nixpkgs> {};
let
  first = runCommand "first" {
    outputHash = "";
    outputHashAlgo = "sha256";
    outputHashMode = "flat";
  } "printf '%s\\n' first > $out";
  second = runCommand "second" {
    outputHash = "";
    outputHashAlgo = "sha256";
    outputHashMode = "flat";
  } "printf '%s\\n' second > $out";
in runCommand "combined" {} "cat ${first} ${second} > $out"
""",
            nixpkgs_path,
        ),
    )
    cmd = parse(["build", "--file", str(nix_file), "--update-fod", *shared_nix_environment.pynix_store_args()])

    await cmd.run()

    data = json.loads(capsys.readouterr().out)
    assert data["updatedFods"] == 2
    assert data["outputs"]["out"]
    assert nix_file.read_text().count('outputHash = "sha256-') == 2


async def test_eval_only_finds_fixed_output_dependencies_in_derivation_closure(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
) -> None:
    """String context hides FOD values, but ``inputDrvs`` retains their identity."""
    nix_file = tmp_path / "source.nix"
    nix_file.write_text(
        with_nixpkgs(
            """with import <nixpkgs> {};
let
  first = runCommand "first" {
    outputHash = "";
    outputHashAlgo = "sha256";
    outputHashMode = "flat";
  } "printf '%s\\n' first > $out";
  second = runCommand "second" {
    outputHash = "";
    outputHashAlgo = "sha256";
    outputHashMode = "flat";
  } "printf '%s\\n' second > $out";
in runCommand "combined" {} "cat ${first} ${second} > $out"
""",
            nixpkgs_path,
        ),
    )

    async with shared_nix_environment.rpc_session() as nix, nix.store() as store, nix.eval(store) as eval:
        root = await eval.file(str(nix_file))
        fixed_output_values: set[str] = set()
        await _fixed_output_derivations_in_value(root, set(), fixed_output_values)

        root_drv_path = await root.attr("drvPath").to_python()
        if not isinstance(root_drv_path, str):
            raise TypeError("root derivation drvPath was not a string")
        fixed_output_closure = await _fixed_output_derivations_in_closure(store, root_drv_path)

    assert fixed_output_values == set()
    assert {"first.drv", "second.drv"} <= {path.rsplit("-", 1)[-1] for path in fixed_output_closure}


async def test_build_update_fod_reports_an_ambiguous_hash_literal(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two identical empty hash literals with no derivation_name to
    disambiguate them (a fetchurl mismatch carries no drv_path) must refuse
    to guess, surfacing find_fod_hash_literal's ambiguity error."""
    payload_a = tmp_path / "payload-a"
    payload_a.write_text("payload a\n")
    payload_b = tmp_path / "payload-b"
    payload_b.write_text("payload b\n")
    nix_file = tmp_path / "source.nix"
    nix_file.write_text(
        f'{{ a = builtins.fetchurl {{ url = "file://{payload_a}"; sha256 = ""; }};'
        f' b = builtins.fetchurl {{ url = "file://{payload_b}"; sha256 = ""; }}; }}\n',
    )
    cmd = parse(
        ["build", "--file", str(nix_file), "--attr", "a", "--update-fod", *shared_nix_environment.pynix_store_args()],
    )

    with pytest.raises(SystemExit):
        await cmd.run()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "cannot update fixed-output hash" in strip_ansi(captured.err)


@LINUX_CHROOT_BUILD
async def test_build_target_raises_when_mismatch_is_not_in_the_target_closure(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mismatch_is_target_fod returning False must abort the update instead
    of patching a hash literal for a derivation outside the evaluated
    target's own closure."""
    nix_file = tmp_path / "source.nix"
    nix_file.write_text(
        with_nixpkgs(
            """with import <nixpkgs> {};
runCommand "payload" {
  outputHash = "";
  outputHashAlgo = "sha256";
  outputHashMode = "flat";
} "printf '%s\\n' fixed-output-payload > $out"
""",
            nixpkgs_path,
        ),
    )
    target = EvaluationTarget(file=str(nix_file), attr=None, flake=None)

    async def _always_false(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(nanopynix_helpers_build, "mismatch_is_target_fod", _always_false)

    async with shared_nix_environment.rpc_session() as nix, nix.store() as store, nix.eval(store) as session:
        with pytest.raises(BuildTargetError, match="not found in the evaluated target derivation closure"):
            await _build_target(target, session, nix=nix, evaluation_store=store, update_fod=True, dry_run=False)


async def test_build_target_requires_a_file_target_to_update_a_fod(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
) -> None:
    """The CLI already refuses --update-fod without --file before evaluation
    (see test_build_update_fod_without_file_errors); this pins
    _build_target's own defense-in-depth check by calling it directly with a
    flake target, bypassing that earlier CLI guard."""
    payload = tmp_path / "payload"
    payload.write_text("fixed-output payload\n")
    flake_dir = tmp_path / "flake"
    flake_dir.mkdir()
    init_flake_repo(flake_dir, f'default = builtins.fetchurl {{ url = "file://{payload}"; sha256 = ""; }};')
    target = EvaluationTarget(file=None, attr=None, flake=f"{flake_dir}#default")

    async with shared_nix_environment.rpc_session() as nix, nix.store() as store, nix.eval(store) as session:
        with pytest.raises(BuildTargetError, match="--update-fod currently requires --file"):
            await _build_target(target, session, nix=nix, evaluation_store=store, update_fod=True, dry_run=False)


@LINUX_CHROOT_BUILD
async def test_build_target_stops_after_ten_fixed_output_hash_updates(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replace_fod_hash that always writes back the same wrong hash
    simulates a FOD that never actually gets fixed, exercising the
    update-count cap instead of looping forever."""
    nix_file = tmp_path / "source.nix"
    nix_file.write_text(
        with_nixpkgs(
            """with import <nixpkgs> {};
runCommand "payload" {
  outputHash = "";
  outputHashAlgo = "sha256";
  outputHashMode = "flat";
} "printf '%s\\n' fixed-output-payload > $out"
""",
            nixpkgs_path,
        ),
    )
    target = EvaluationTarget(file=str(nix_file), attr=None, flake=None)

    def _rewrite_with_still_wrong_hash(source: str, literal: Any, _got: str) -> str:
        return _real_replace_fod_hash(source, literal, "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

    monkeypatch.setattr(nanopynix_helpers_build, "replace_fod_hash", _rewrite_with_still_wrong_hash)

    async with shared_nix_environment.rpc_session() as nix, nix.store() as store, nix.eval(store) as session:
        with pytest.raises(BuildTargetError, match="stopped after 10 fixed-output hash updates"):
            await _build_target(target, session, nix=nix, evaluation_store=store, update_fod=True, dry_run=False)
