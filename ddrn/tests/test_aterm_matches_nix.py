"""Check the writer of ``ddrn`` against Nix itself.

``ddrn`` writes ATerm in pure Python, because a planner runs inside a build
sandbox where the closure of libnixstore is a cost with no benefit. A copy of
a format is only safe while something proves that the copy agrees with the
original. That is what this module does:

1. Write a derivation with ``ddrn``.
2. Add the text to the store, and let **Nix** parse it, through
   ``nanopynix``'s ``read_derivation``.
3. Rebuild the derivation from what Nix reported, and let **Nix** write it
   again with its own ATerm writer. The two texts must be equal byte for byte.

Step 2 uses the parser of Nix, and step 3 uses the writer of Nix. Neither one
is a second Python implementation, so a disagreement about the format is a
failure here. Step 2 alone would miss a difference that the parser tolerates,
such as a field in the wrong order or an escape that decodes to the same
string.

Run it with ``direnv exec . pytest ddrn/tests``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import pytest
from nanopynix_bindings import store as nanopynix_store

import ddrn
import nanopynix
from nanopynix.settings import NixSettings

if TYPE_CHECKING:
    from pathlib import Path

#: The SHA-256 of nothing at all, which is a valid hash and needs no fixture.
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

# A sample of the shapes that a planner emits. Each one exercises a part of
# the format that the others do not.
CASES = [
    pytest.param(
        ddrn.Derivation(
            name="plain",
            system="x86_64-linux",
            builder="/nix/store/00000000000000000000000000000000-bash/bin/bash",
            args=["-c", "true"],
        ),
        id="floating-ca-no-inputs",
    ),
    pytest.param(
        ddrn.Derivation(
            name="quoting",
            system="x86_64-linux",
            builder="/nix/store/00000000000000000000000000000000-bash/bin/bash",
            args=["-c", 'printf "%s\\n" "a\tb"\nnewline'],
            env={"QUOTED": 'a "b" \\ c', "NEWLINE": "one\ntwo", "TAB": "a\tb"},
        ),
        id="escaping",
    ),
    # Nix recomputes the output path of a fixed-output derivation and refuses
    # the derivation when its own answer differs. This case therefore checks
    # `make_fixed_output_path` against Nix, and not only the ATerm text.
    pytest.param(
        ddrn.Derivation(
            name="fixed-output",
            system="x86_64-linux",
            builder="/nix/store/00000000000000000000000000000000-bash/bin/bash",
            args=["-c", "curl -o $out $URL"],
            env={"URL": "https://example.invalid/x.whl"},
            outputs={"out": ddrn.Output.fixed("fixed-output", sha256=EMPTY_SHA256, method="nar")},
        ),
        id="fixed-output-nar",
    ),
    pytest.param(
        ddrn.Derivation(
            name="fixed-flat",
            system="x86_64-linux",
            builder="/nix/store/00000000000000000000000000000000-bash/bin/bash",
            args=["-c", "curl -o $out $URL"],
            env={"URL": "https://example.invalid/x.whl"},
            outputs={"out": ddrn.Output.fixed("fixed-flat", sha256=EMPTY_SHA256)},
        ),
        id="fixed-output-flat",
    ),
]


# The test is `async def`, and it does not call `anyio.run`. `anyio_mode` is
# `auto` in `pytest.ini`, so pytest runs the coroutine itself. A nested
# `anyio.run` raises "Already running asyncio in this thread" as soon as
# something else in the same process already holds a loop.
@pytest.mark.parametrize("derivation", CASES)
async def test_nix_parses_what_ddrn_wrote(derivation: ddrn.Derivation, tmp_path: Path) -> None:
    """Nix reads back every field that ``ddrn`` wrote, unchanged."""
    aterm = derivation.to_aterm()
    source = anyio.Path(tmp_path) / f"{derivation.name}.drv"
    await source.write_text(aterm, encoding="utf-8")

    async with (
        nanopynix.rpc.Session(settings=NixSettings()) as session,
        session.store() as store,
    ):
        # `add_to_store` puts the text where Nix can read it. The name has to
        # end in `.drv`, or `read_derivation` refuses the path.
        path = await store.add_to_store(str(source), name=f"{derivation.name}.drv")
        parsed = await store.read_derivation(path)

    assert parsed.name == derivation.name
    assert parsed.system == derivation.system
    assert parsed.builder == derivation.builder
    assert list(parsed.args) == list(derivation.args)
    assert dict(parsed.env) == derivation.environment()


@pytest.mark.parametrize("derivation", CASES)
def test_nix_writes_back_the_same_bytes(derivation: ddrn.Derivation, tmp_path: Path) -> None:
    """Nix's own ATerm writer produces exactly what ``ddrn`` produced.

    This is the stronger half of the check. The test above proves that Nix
    reads each field back. This one proves that Nix, given those fields,
    writes the same bytes, so the order of the fields and the escaping of
    each string agree too.

    Every call here is synchronous, and none of them opens an evaluator, so
    this test uses the bindings directly rather than a ``Session``.
    """
    aterm = derivation.to_aterm()
    source = tmp_path / f"{derivation.name}.drv"
    source.write_text(aterm, encoding="utf-8")

    # `open_store` aborts the process when libstore has no settings yet, and
    # nothing else in this module opens a store through the bindings. The call
    # is idempotent.
    nanopynix.init_libstore()
    store = nanopynix_store.open_store(f"local?root={tmp_path / 'store'}")
    config = nanopynix_store.StoreDirConfig(store.get_store_dir())
    path = store.add_to_store(str(source), name=f"{derivation.name}.drv")

    # `read_derivation` returns the parse of Nix, and `from_dict` builds the
    # `nix::Derivation` that the parse describes. `to_aterm` is the writer
    # that `writeDerivation` uses, so these are the bytes Nix would store.
    parsed = store.read_derivation(path)
    assert nanopynix_store.Derivation.from_dict(config, parsed).to_aterm(config) == aterm


def test_reference_scan_finds_every_store_path() -> None:
    """``referenced_paths`` reports a path wherever it appears.

    The check that uses it runs before Nix does, so a path it misses becomes a
    build failure with no line number.
    """
    bash = "/nix/store/00000000000000000000000000000000-bash"
    tool = "/nix/store/11111111111111111111111111111111-coreutils"
    wheel = "/nix/store/22222222222222222222222222222222-x.whl"
    drv = "/nix/store/33333333333333333333333333333333-x.drv"

    derivation = ddrn.Derivation(
        name="scan",
        system="x86_64-linux",
        builder=f"{bash}/bin/bash",
        args=["-c", f"cat {wheel}/inner/file"],
        env={"TOOL": f"{tool}/bin/cat"},
        input_srcs=[bash],
        input_drvs={drv: ["out"]},
    )

    # Each path is reported as its root, and never as the file inside it.
    assert derivation.referenced_paths() == {bash, tool, wheel, drv}


def test_flat_fixed_output_path_rejects_references() -> None:
    """A flat fixed output has no references, so asking for one is an error."""
    with pytest.raises(ValueError, match="carries no references"):
        ddrn.make_fixed_output_path(
            "/nix/store",
            "x",
            sha256=EMPTY_SHA256,
            recursive=False,
            references=["/nix/store/00000000000000000000000000000000-bash"],
        )


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("sha256-47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=", EMPTY_SHA256),
        (f"sha256:{EMPTY_SHA256}", EMPTY_SHA256),
        (EMPTY_SHA256, EMPTY_SHA256),
    ],
)
def test_sri_to_hex_reads_each_spelling(written: str, expected: str) -> None:
    """A lock file writes a hash in one of three spellings, and all three work."""
    assert ddrn.sri_to_hex(written) == expected
