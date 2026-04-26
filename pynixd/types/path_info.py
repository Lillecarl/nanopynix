"""Path info and metadata domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..store_path import StorePath

if TYPE_CHECKING:
    from ..wire import NixReader, NixWriter


@dataclass
class UnkeyedValidPathInfo:
    """Metadata for a store path (without the path itself)."""

    deriver: StorePath = field(default_factory=lambda: StorePath(""))
    nar_hash: str = ""
    references: set[StorePath] = field(default_factory=set)
    registration_time: int = 0
    nar_size: int = 0
    ultimate: int = 0
    sigs: set[str] = field(default_factory=set)
    ca: str = ""

    async def from_reader(self, reader: NixReader) -> UnkeyedValidPathInfo:
        self.deriver = await reader.read_string(StorePath)
        self.nar_hash = await reader.read_string()
        self.references = await reader.read_string_set(StorePath)
        self.registration_time = await reader.read_uint64()
        self.nar_size = await reader.read_uint64()
        self.ultimate = await reader.read_uint64()
        self.sigs = await reader.read_string_set()
        self.ca = await reader.read_string()
        return self

    def to_writer(self, writer: NixWriter) -> None:
        writer.write_string(self.deriver)
        nar_hash = self.nar_hash
        nar_hash = nar_hash.removeprefix("sha256:")
        writer.write_string(nar_hash)
        writer.write_string_set(self.references)
        writer.write_uint64(self.registration_time)
        writer.write_uint64(self.nar_size)
        writer.write_uint64(self.ultimate)
        writer.write_string_set(self.sigs)
        writer.write_string(self.ca)

    def with_path(self, path: StorePath) -> ValidPathInfo:
        return ValidPathInfo(
            path=path,
            deriver=self.deriver,
            nar_hash=self.nar_hash,
            references=self.references,
            registration_time=self.registration_time,
            nar_size=self.nar_size,
            ultimate=self.ultimate,
            sigs=self.sigs,
            ca=self.ca,
        )


@dataclass
class ValidPathInfo(UnkeyedValidPathInfo):
    """Metadata for a store path (including the path)."""

    path: StorePath = field(default_factory=lambda: StorePath(""))

    def __hash__(self) -> int:
        return hash(self.path)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ValidPathInfo):
            return False
        return self.path == other.path

    async def from_reader(self, reader: NixReader) -> ValidPathInfo:
        path = await reader.read_string(StorePath)
        info = await UnkeyedValidPathInfo().from_reader(reader)
        return info.with_path(path)

    def to_writer(self, writer: NixWriter) -> None:
        writer.write_string(self.path)
        super().to_writer(writer)

    def to_bytes(self) -> bytes:
        from .. import wire

        buf = wire.BytesWriter()
        self.to_writer(buf)
        return buf.get_bytes()

    @classmethod
    def from_narinfo(cls, content: str) -> ValidPathInfo:
        data: dict[str, Any] = {
            "references": set(),
            "sigs": set(),
        }
        for line in content.splitlines():
            line = line.strip()
            if not line or ":" not in line or line.startswith("#"):
                continue
            key, val = line.split(":", 1)
            key = key.strip()
            val = val.strip()

            if key == "StorePath":
                data["path"] = StorePath(val)
            elif key == "NarHash":
                data["nar_hash"] = val
            elif key == "NarSize":
                data["nar_size"] = int(val)
            elif key == "References":
                for r in val.split():
                    if r:
                        if r.startswith("/nix/store/"):
                            data["references"].add(StorePath(r))
                        else:
                            data["references"].add(StorePath(f"/nix/store/{r}"))
            elif key == "Deriver":
                if val:
                    if val.startswith("/nix/store/"):
                        data["deriver"] = StorePath(val)
                    else:
                        data["deriver"] = StorePath(f"/nix/store/{val}")
                else:
                    data["deriver"] = StorePath("")
            elif key == "Sig":
                data["sigs"].add(val)
            elif key == "CA":
                data["ca"] = val

        return cls(
            path=data.get("path", StorePath("")),
            deriver=data.get("deriver", StorePath("")),
            nar_hash=data.get("nar_hash", ""),
            references=data.get("references", set()),
            nar_size=data.get("nar_size", 0),
            sigs=data.get("sigs", set()),
            ca=data.get("ca", ""),
        )

    def to_narinfo(self) -> str:
        nar_hash = self.nar_hash
        if not nar_hash.startswith("sha256:"):
            nar_hash = f"sha256:{nar_hash}"

        nar_hash_part = nar_hash.split(":")[-1]

        lines = [
            f"StorePath: {self.path}",
            f"URL: nar/{nar_hash_part}.nar",
            "Compression: none",
            f"NarHash: {nar_hash}",
            f"NarSize: {self.nar_size}",
        ]

        if self.references:

            def strip_prefix(p: str) -> str:
                return p.split("/")[-1]

            refs = " ".join(sorted(strip_prefix(str(r)) for r in self.references))
            lines.append(f"References: {refs}")

        if self.deriver:
            lines.append(f"Deriver: {self.deriver.split('/')[-1]}")

        lines.extend(f"Sig: {sig}" for sig in sorted(self.sigs))

        if self.ca:
            lines.append(f"CA: {self.ca}")

        return "\n".join(lines) + "\n"


@dataclass
class SubstitutablePathInfo:
    """Metadata for a substitutable path (missing but available)."""

    deriver: StorePath = field(default_factory=lambda: StorePath(""))
    references: set[StorePath] = field(default_factory=set)
    download_size: int = 0
    nar_size: int = 0

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
    ) -> SubstitutablePathInfo:
        self.deriver = await reader.read_string(StorePath)
        self.references = await reader.read_string_set(StorePath)
        self.download_size = await reader.read_uint64()
        self.nar_size = await reader.read_uint64()
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string(self.deriver)
        writer.write_string_set(self.references)
        writer.write_uint64(self.download_size)
        writer.write_uint64(self.nar_size)
