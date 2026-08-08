"""Check the writer of ``ddrn`` against Nix itself.

``ddrn`` writes ATerm in pure Python, because a planner runs inside a build
sandbox where the closure of libnixstore is a cost with no benefit. A copy of
a format is only safe while something proves that the copy agrees with the
original. That is what this module does:

1. Write a derivation with ``ddrn``.
2. Add the text to the store, and let **Nix** parse it, through
   ``nanopynix``'s ``read_derivation``.
3. Rebuild the derivation from what Nix reported, and compare the two ATerm
   texts byte for byte.

Step 2 uses the parser of Nix and not a second Python implementation, so a
disagreement about the format is a failure here.

Run it with ``direnv exec . pytest ddrn/tests``.
"""

from __future__ import annotations

import anyio
import pytest

import ddrn
import nanopynix
from nanopynix.settings import NixSettings

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


@pytest.mark.parametrize("derivation", CASES)
def test_nix_parses_what_ddrn_wrote(derivation: ddrn.Derivation, tmp_path: anyio.Path) -> None:
    """Nix reads back every field that ``ddrn`` wrote, unchanged."""

    async def run() -> None:
        aterm = derivation.to_aterm()
        source = anyio.Path(tmp_path) / f"{derivation.name}.drv"
        await source.write_text(aterm, encoding="utf-8")

        async with (
            nanopynix.rpc.Session(settings=NixSettings()) as session,
            session.store() as store,
        ):
            # `add_to_store` puts the text where Nix can read it. The name has
            # to end in `.drv`, or `read_derivation` refuses the path.
            path = await store.add_to_store(str(source), name=f"{derivation.name}.drv")
            parsed = await store.read_derivation(path)

        assert parsed.name == derivation.name
        assert parsed.system == derivation.system
        assert parsed.builder == derivation.builder
        assert list(parsed.args) == list(derivation.args)
        assert dict(parsed.env) == derivation.environment()

    anyio.run(run)


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
