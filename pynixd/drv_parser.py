"""
Parser for Nix .drv files (ATerm format).

Parses the ATerm representation into a structured Derivation.
Handles Derive(...) and DrvWithVersion("xp-dyn-drv",...) formats.

ATerm .drv format (Traditional Derive):
    Derive(
        [("out","/nix/store/...","",""),...],       -- outputs
        [("/nix/store/...drv",["out"]),...],        -- inputDrvs (simple)
        ["/nix/store/..."],                          -- inputSrcs
        "x86_64-linux",                              -- platform
        "/nix/store/.../bin/bash",                   -- builder
        ["-e","/nix/store/.../builder.sh"],          -- args
        [("key","value"),...]                        -- env
    )

ATerm .drv format (Dynamic DrvWithVersion):
    DrvWithVersion("xp-dyn-drv",
        [("out","/nix/store/...","",""),...],       -- outputs (same as above)
        [("/nix/store/...drv",(["out"],[...nested...])),...], -- inputDrvs (dynamic)
        ...
    )

Output fields: (name, path, hash_algo, hash_value)

Compatibility with DerivationOutput (operations/base.py):
  OutputInfo:  name, path, hash_algo, hash_value  (parser - raw ATerm fields)
  DerivationOutput: name, path, method, hash_digest  (wire protocol)
  Mapping: name->name, path->path, hash_algo->method, hash_value->hash_digest
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, TypedDict

import anyio

from .store_path import StorePath
from .types import BasicDerivation, DerivationOutput, OutputKind

if TYPE_CHECKING:
    from pathlib import Path

    from .types.aliases import OutputMap, StorePathSet


class NixDerivationOutputShow(TypedDict, total=False):
    """Output entry in `nix derivation show` JSON."""

    path: str
    hashAlgo: str
    hash: str


class NixInputDrvShow(TypedDict):
    """Input derivation entry in `nix derivation show` JSON."""

    dynamicOutputs: dict[str, dict[str, list[str]]]
    outputs: list[str]


class NixDerivationShow(TypedDict):
    """Complete derivation object in `nix derivation show` JSON."""

    args: list[str]
    builder: str
    env: dict[str, str]
    inputDrvs: dict[str, NixInputDrvShow]
    inputSrcs: list[str]
    name: str
    outputs: dict[str, NixDerivationOutputShow]
    system: str


def _aterm_escape(s: str) -> str:
    s = s.replace("\\", "\\\\")
    s = s.replace('"', '\\"')
    s = s.replace("\n", "\\n")
    s = s.replace("\r", "\\r")
    return s.replace("\t", "\\t")


@dataclass
class OutputInfo:
    """A single derivation output from ATerm parsing."""

    name: str
    path: str
    hash_algo: str
    hash_value: str


@dataclass
class Derivation:
    """A parsed .drv file."""

    outputs: list[OutputInfo] = field(default_factory=list)

    input_drvs: dict[StorePath, list[str]] = field(default_factory=dict)

    input_srcs: StorePathSet = field(default_factory=set)

    platform: str = ""
    builder: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

    is_dynamic: bool = False
    """True if DrvWithVersion("xp-dyn-drv",...) format (dynamic derivations)."""

    dynamic_input_drvs: dict[StorePath, dict[str, list[str]]] = field(
        default_factory=dict,
    )
    # dynamic_input_drvs: {drv_path: {output_name: [nested_output_name, ...], ...}}
    # Only present for DrvWithVersion format where outputs depend on
    # other dynamic outputs

    @property
    def required_system_features(self) -> set[str]:
        """Parse requiredSystemFeatures from the env dict.

        Nix encodes this as a space-separated string in the derivation
        environment, e.g. ``"recursive-nix uid-range"``.
        """
        raw = self.env.get("requiredSystemFeatures", "")
        if not raw:
            return set()
        return set(raw.split())

    def output_paths(self) -> dict[str, StorePath]:
        """Return {output_name: output_path} for all outputs."""
        return {o.name: StorePath(o.path) for o in self.outputs}

    def output_kinds(self) -> list[OutputKind]:
        """Return the OutputKind for each output.

        Avoids callers needing to construct DerivationOutput objects.
        """
        result: list[OutputKind] = []
        for o in self.outputs:
            dop = DerivationOutput(
                path=o.path,
                method=o.hash_algo,
                hash_digest=o.hash_value,
            )
            result.append(dop.kind)
        return result

    def to_json(self, drv_path: StorePath | str) -> dict[str, NixDerivationShow]:
        """Serialize to the same JSON format as `nix derivation show`.

        Args:
            drv_path: The store path of this .drv file (used as top-level key).
        """
        drv_path_str = str(drv_path)
        # Outputs: {name: {path, hash?, hashAlgo?}}
        outputs: dict[str, NixDerivationOutputShow] = {}
        for o in self.outputs:
            entry: NixDerivationOutputShow = {}
            if o.path:
                entry["path"] = o.path
            if o.hash_algo:
                entry["hashAlgo"] = o.hash_algo
            if o.hash_value:
                entry["hash"] = o.hash_value
            outputs[o.name] = entry

        input_drvs: dict[str, NixInputDrvShow] = {}
        # Merge simple and dynamic entries
        all_drv_paths = set(self.input_drvs) | set(self.dynamic_input_drvs)
        for dp in sorted(all_drv_paths):
            entry_outputs = self.input_drvs.get(dp, [])
            dynamic = self.dynamic_input_drvs.get(dp, {})
            dynamic_out: dict[str, dict[str, list[str]]] = {}
            for out_name, nested in dynamic.items():
                dynamic_out[out_name] = {"outputs": nested}
            input_drvs[str(dp)] = {
                "dynamicOutputs": dynamic_out,
                "outputs": entry_outputs,
            }

        # name is derived from the store path: /nix/store/<hash>-<name>.drv
        basename = drv_path_str.rsplit("/", 1)[-1]  # <hash>-<name>.drv
        name = basename.split("-", 1)[1] if "-" in basename else basename
        name = name.removesuffix(".drv")

        inner: NixDerivationShow = {
            "args": self.args,
            "builder": self.builder,
            "env": self.env,
            "inputDrvs": input_drvs,
            "inputSrcs": sorted(str(p) for p in self.input_srcs),
            "name": name,
            "outputs": outputs,
            "system": self.platform,
        }
        return {drv_path_str: inner}

    def serialize(self) -> str:
        """Serialize to ATerm format (the on-disk .drv representation).

        Returns a string in either ``Derive(...)`` or
        ``DrvWithVersion("xp-dyn-drv", ...)`` format.
        """
        if self.is_dynamic:
            return self._serialize_dynamic()
        return self._serialize_traditional()

    def _serialize_traditional(self) -> str:
        parts: list[str] = ["Derive("]

        # Serialize outputs
        out_parts = [
            f'("{_aterm_escape(o.name)}","{_aterm_escape(o.path)}",'
            f'"{_aterm_escape(o.hash_algo)}","{_aterm_escape(o.hash_value)}")'
            for o in sorted(self.outputs, key=lambda x: x.name)
        ]
        parts.append(f"[{','.join(out_parts)}],")

        # Serialize input derivations
        drv_parts = [
            f'("{_aterm_escape(str(drv_path))}",[{",".join(f'"{_aterm_escape(o)}"' for o in outputs)}])'
            for drv_path, outputs in sorted(self.input_drvs.items(), key=lambda x: str(x[0]))
        ]
        parts.append(f"[{','.join(drv_parts)}],")

        # Serialize input sources
        srcs = ",".join(f'"{_aterm_escape(str(p))}"' for p in sorted(str(p) for p in self.input_srcs))
        parts.append(f"[{srcs}],")

        parts.append(f'"{_aterm_escape(self.platform)}",')
        parts.append(f'"{_aterm_escape(self.builder)}",')

        args = ",".join(f'"{_aterm_escape(a)}"' for a in self.args)
        parts.append(f"[{args}],")

        env_parts = [f'("{_aterm_escape(k)}","{_aterm_escape(v)}")' for k, v in sorted(self.env.items())]
        parts.append(f"[{','.join(env_parts)}]")

        parts.append(")")
        return "".join(parts)

    def _serialize_dynamic(self) -> str:
        parts: list[str] = ['DrvWithVersion("xp-dyn-drv",']

        # outputs (same as traditional)
        out_parts = [
            f'("{_aterm_escape(o.name)}","{_aterm_escape(o.path)}",'
            f'"{_aterm_escape(o.hash_algo)}","{_aterm_escape(o.hash_value)}")'
            for o in sorted(self.outputs, key=lambda x: x.name)
        ]
        parts.append(f"[{','.join(out_parts)}],")

        # input_drvs — mixed simple and dynamic entries
        def _serialize_drv_entry(drv_path: StorePath) -> str:
            outputs = self.input_drvs.get(drv_path, [])
            dynamic = self.dynamic_input_drvs.get(drv_path)
            out_list = ",".join(f'"{_aterm_escape(o)}"' for o in outputs)

            if dynamic is not None:
                nested_parts = [
                    f'("{_aterm_escape(nested_name)}",[{",".join(f'"{_aterm_escape(d)}"' for d in nested_deps)}])'
                    for nested_name, nested_deps in sorted(dynamic.items())
                ]
                dynamic_out_names = ",".join(f'"{_aterm_escape(k)}"' for k in sorted(dynamic.keys()))
                return f'("{_aterm_escape(str(drv_path))}",([{dynamic_out_names}],[{",".join(nested_parts)}]))'
            return f'("{_aterm_escape(str(drv_path))}",[{out_list}])'

        all_drv_paths = sorted(set(self.input_drvs) | set(self.dynamic_input_drvs), key=str)
        drv_parts = [_serialize_drv_entry(drv_path) for drv_path in all_drv_paths]
        parts.append(f"[{','.join(drv_parts)}],")

        # input_srcs
        srcs = ",".join(f'"{_aterm_escape(str(p))}"' for p in sorted(str(p) for p in self.input_srcs))
        parts.append(f"[{srcs}],")

        parts.append(f'"{_aterm_escape(self.platform)}",')
        parts.append(f'"{_aterm_escape(self.builder)}",')

        args = ",".join(f'"{_aterm_escape(a)}"' for a in self.args)
        parts.append(f"[{args}],")

        env_parts = [f'("{_aterm_escape(k)}","{_aterm_escape(v)}")' for k, v in sorted(self.env.items())]
        parts.append(f"[{','.join(env_parts)}]")

        parts.append(")")
        return "".join(parts)


_STRING_CHUNK = re.compile(r'([^"\\]*)(["\\])')


class _Parser:
    """Simple recursive-descent ATerm parser for .drv files."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._pos = 0

    def _peek(self) -> str:
        if self._pos >= len(self._text):
            raise ValueError("Unexpected end of input")
        return self._text[self._pos]

    def _advance(self) -> str:
        ch = self._peek()
        self._pos += 1
        return ch

    def _expect(self, s: str) -> None:
        for ch in s:
            got = self._advance()
            if got != ch:
                raise ValueError(f"Expected {ch!r} at pos {self._pos - 1}, got {got!r}")

    def _skip_ws(self) -> None:
        while self._pos < len(self._text) and self._text[self._pos] in " \t\n\r":
            self._pos += 1

    _ESCAPE: ClassVar[dict[str, str]] = {
        '"': '"',
        "\\": "\\",
        "/": "/",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
    }

    def parse_string(self) -> str:
        """Parse a quoted ATerm string with escape handling.

        Uses regex to scan chunks of non-special characters at once
        for performance on large strings (e.g. multi-MB env values).
        """
        self._expect('"')
        parts: list[str] = []
        while True:
            m = _STRING_CHUNK.search(self._text, self._pos)
            if m is None:
                raise ValueError(f"Unterminated string starting at pos {self._pos}")
            content, terminator = m.group(1), m.group(2)
            self._pos = m.end()
            if content:
                parts.append(content)
            if terminator == '"':
                return "".join(parts)
            # terminator == '\\' — read escape
            esc = self._advance()
            ch = self._ESCAPE.get(esc)
            if ch is not None:
                parts.append(ch)
            else:
                # Unknown escape — pass through
                parts.append("\\")
                parts.append(esc)

    def parse_string_list[T: str = str](self, tp: type[T] = str) -> list[T]:
        """Parse [str, str, ...]."""
        self._expect("[")
        result: list[T] = []
        self._skip_ws()
        while self._peek() != "]":
            if result:
                self._expect(",")
            self._skip_ws()
            result.append(tp(self.parse_string()))
            self._skip_ws()
        self._expect("]")
        return result

    def parse_outputs(self) -> list[OutputInfo]:
        """Parse [("name","path","hashAlgo","hash"), ...] into OutputInfo list."""
        self._expect("[")
        result: list[OutputInfo] = []
        self._skip_ws()
        while self._peek() != "]":
            if result:
                self._expect(",")
            self._skip_ws()
            self._expect("(")
            name = self.parse_string()
            self._expect(",")
            path = self.parse_string()
            self._expect(",")
            hash_algo = self.parse_string()
            self._expect(",")
            hash_value = self.parse_string()
            self._expect(")")
            result.append(
                OutputInfo(
                    name=name,
                    path=path,
                    hash_algo=hash_algo,
                    hash_value=hash_value,
                ),
            )
            self._skip_ws()
        self._expect("]")
        return result

    def parse_input_drvs_simple(self) -> dict[StorePath, list[str]]:
        """Parse [("drvPath",["out",...]), ...] - traditional format."""
        self._expect("[")
        result: dict[StorePath, list[str]] = {}
        self._skip_ws()
        while self._peek() != "]":
            if result:
                self._expect(",")
            self._skip_ws()
            self._expect("(")
            drv_path = StorePath(self.parse_string())
            self._expect(",")
            outputs = self.parse_string_list()
            self._expect(")")
            result[drv_path] = outputs
            self._skip_ws()
        self._expect("]")
        return result

    def parse_input_drvs_dynamic(
        self,
    ) -> tuple[dict[StorePath, list[str]], dict[StorePath, dict[str, list[str]]]]:
        """Parse dynamic input drvs format.

        Returns:
            (simple_map, dynamic_map) where:
            - simple_map: {drv_path: [output_name, ...]} for non-dynamic inputs
            - dynamic_map: {drv_path: {output_name: [nested_output_name, ...], ...}}
              for inputs that depend on dynamic outputs
        """
        self._expect("[")
        simple: dict[StorePath, list[str]] = {}
        dynamic: dict[StorePath, dict[str, list[str]]] = {}
        self._skip_ws()
        while self._peek() != "]":
            if simple or dynamic:
                self._expect(",")
            self._skip_ws()
            self._expect("(")
            drv_path = StorePath(self.parse_string())
            self._expect(",")
            self._skip_ws()

            if self._peek() == "[":
                # Non-dynamic: simple list of output names
                outputs = self.parse_string_list()
                simple[drv_path] = outputs
            elif self._peek() == "(":
                # Dynamic: nested structure
                self._advance()  # '('
                # First part: output names
                self.parse_string_list()
                self._expect(",")
                self._expect("[")  # Start of nested structure
                nested: dict[str, list[str]] = {}
                self._skip_ws()
                while self._peek() != "]":
                    if nested:
                        self._expect(",")
                    self._skip_ws()
                    self._expect("(")
                    nested_output_name = self.parse_string()
                    self._expect(",")
                    # Recursive nested for deeper dynamic deps
                    nested_deps = self.parse_string_list()
                    nested[nested_output_name] = nested_deps
                    self._expect(")")
                    self._skip_ws()
                self._expect("]")  # End of nested structure
                self._expect(")")  # End of dynamic entry
                dynamic[drv_path] = nested
            else:
                raise ValueError(
                    f"Expected '[' or '(' at pos {self._pos}, got {self._peek()!r}",
                )
            self._expect(")")
            self._skip_ws()
        self._expect("]")
        return simple, dynamic

    def parse_env(self) -> dict[str, str]:
        """Parse [("key","value"), ...]."""
        self._expect("[")
        result: dict[str, str] = {}
        self._skip_ws()
        while self._peek() != "]":
            if result:
                self._expect(",")
            self._skip_ws()
            self._expect("(")
            key = self.parse_string()
            self._expect(",")
            value = self.parse_string()
            self._expect(")")
            result[key] = value
            self._skip_ws()
        self._expect("]")
        return result

    def parse_derivation(self) -> Derivation:
        """Parse the full Derive(...) or DrvWithVersion(...) term."""
        self._skip_ws()

        # Check for dynamic derivation format
        if self._text[self._pos :].startswith("DrvWithVersion("):
            return self._parse_dynamic_derivation()
        return self._parse_traditional_derivation()

    def _parse_traditional_derivation(self) -> Derivation:
        """Parse Derive(...) term."""
        self._expect("Derive(")
        self._skip_ws()

        outputs = self.parse_outputs()
        self._expect(",")
        self._skip_ws()

        input_drvs = self.parse_input_drvs_simple()
        self._expect(",")
        self._skip_ws()

        input_srcs_list = self.parse_string_list(StorePath)
        self._expect(",")
        self._skip_ws()

        platform = self.parse_string()
        self._expect(",")
        self._skip_ws()

        builder = self.parse_string()
        self._expect(",")
        self._skip_ws()

        args = self.parse_string_list()
        self._expect(",")
        self._skip_ws()

        env = self.parse_env()
        self._skip_ws()
        self._expect(")")

        return Derivation(
            outputs=outputs,
            input_drvs=input_drvs,
            input_srcs=set(input_srcs_list),
            platform=platform,
            builder=builder,
            args=args,
            env=env,
            is_dynamic=False,
        )

    def _parse_dynamic_derivation(self) -> Derivation:
        """Parse DrvWithVersion("xp-dyn-drv",...) term."""
        self._expect("DrvWithVersion(")
        self._skip_ws()

        version = self.parse_string()
        if version != "xp-dyn-drv":
            raise ValueError(f"Unknown derivation ATerm version: {version!r}")
        self._expect(",")
        self._skip_ws()

        outputs = self.parse_outputs()
        self._expect(",")
        self._skip_ws()

        input_drvs, dynamic_input_drvs = self.parse_input_drvs_dynamic()
        self._expect(",")
        self._skip_ws()

        input_srcs_list = self.parse_string_list(StorePath)
        self._expect(",")
        self._skip_ws()

        platform = self.parse_string()
        self._expect(",")
        self._skip_ws()

        builder = self.parse_string()
        self._expect(",")
        self._skip_ws()

        args = self.parse_string_list()
        self._expect(",")
        self._skip_ws()

        env = self.parse_env()
        self._skip_ws()
        self._expect(")")

        return Derivation(
            outputs=outputs,
            input_drvs=input_drvs,
            input_srcs=set(input_srcs_list),
            platform=platform,
            builder=builder,
            args=args,
            env=env,
            is_dynamic=True,
            dynamic_input_drvs=dynamic_input_drvs,
        )


async def to_basic_derivation(
    parsed: Derivation,
    store_path: Path,
    output_cache: OutputMap | None = None,
) -> BasicDerivation:
    """Convert a Derivation to a BasicDerivation (wire protocol format).

    Resolves inputDrvs into concrete output paths and merges them into
    input_srcs, matching what nix does when sending BuildDerivation over
    the wire.

    Args:
        parsed: The parsed .drv file
        store_path: Store root for reading referenced .drv files
        output_cache: Optional {drv_path: {output_name: output_path}} cache
            from the DB to skip reading input .drv files from disk.
    """
    outputs = {
        o.name: DerivationOutput(
            path=o.path,
            method=o.hash_algo,
            hash_digest=o.hash_value,
        )
        for o in parsed.outputs
    }

    # Start with the explicit input sources
    input_srcs: StorePathSet = set(parsed.input_srcs)

    # Resolve inputDrvs: for each input drv, look up its output paths
    # and add them to input_srcs (this is what nix does before sending
    # BuildDerivation over the wire)
    for drv_path, output_names in parsed.input_drvs.items():
        # Try cache first (from DB)
        if output_cache and drv_path in output_cache:
            cached = output_cache[drv_path]
            for name in output_names:
                p = cached.get(name)
                if p:
                    input_srcs.add(p)
            continue

        # Fall back to reading the .drv file
        try:
            input_parsed = await read_drv_file(store_path, drv_path)
        except FileNotFoundError:
            # Can't resolve — add the drv itself as a dependency
            input_srcs.add(StorePath(drv_path))
            continue
        all_outputs = input_parsed.output_paths()
        for name in output_names:
            p = all_outputs.get(name)
            if p:
                input_srcs.add(p)

    return BasicDerivation(
        outputs=outputs,
        input_srcs=input_srcs,
        platform=parsed.platform,
        builder=parsed.builder,
        args=parsed.args,
        env=parsed.env,
        is_dynamic=parsed.is_dynamic,
    )


def parse_drv(content: str) -> Derivation:
    """Parse a .drv file's content into a Derivation."""
    return _Parser(content).parse_derivation()


async def read_drv_file(
    store_path: Path,
    drv_store_path: StorePath | str,
) -> Derivation:
    """Read and parse a .drv file from a store's filesystem.

    Args:
        store_path: The store root (e.g., "/tmp/pynixd-test-local")
        drv_store_path: The full store path (e.g., "/nix/store/xxx.drv")

    Returns:
        Parsed derivation
    """
    # drv_store_path is like "/nix/store/xxx.drv"
    # On disk it's at "{store_path}/nix/store/xxx.drv"
    fs_path = store_path / str(drv_store_path).lstrip("/")
    content = await anyio.Path(fs_path).read_text()
    return parse_drv(content)
