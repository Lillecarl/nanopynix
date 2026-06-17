"""Path info and metadata domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Self

from .. import wire
from ..store_path import StorePath
from .context import ReadContext, WriteContext

if TYPE_CHECKING:
    from .aliases import ContentAddress, NARHash, StorePathSet


@dataclass(kw_only=True)
class UnkeyedValidPathInfo:
    """Metadata for a store path (without the path itself)."""

    deriver: StorePath = field(default_factory=lambda: StorePath(""))
    nar_hash: NARHash = ""
    references: StorePathSet = field(default_factory=set)
    registration_time: int = 0
    nar_size: int = 0
    ultimate: int = 0
    sigs: set[str] = field(default_factory=set)
    ca: ContentAddress = ""

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.deriver = await ctx.reader.read_string(StorePath)
        obj.nar_hash = await ctx.reader.read_string()
        obj.references = await ctx.reader.read_string_set(StorePath)
        obj.registration_time = await ctx.reader.read_uint64()
        obj.nar_size = await ctx.reader.read_uint64()
        obj.ultimate = await ctx.reader.read_uint64()
        obj.sigs = await ctx.reader.read_string_set()
        obj.ca = await ctx.reader.read_string()
        return obj

    def serialize(self, ctx: WriteContext) -> None:
        ctx.writer.write_string(self.deriver)
        nar_hash = self.nar_hash
        nar_hash = nar_hash.removeprefix("sha256:")
        ctx.writer.write_string(nar_hash)
        ctx.writer.write_string_set(self.references)
        ctx.writer.write_uint64(self.registration_time)
        ctx.writer.write_uint64(self.nar_size)
        ctx.writer.write_uint64(self.ultimate)
        ctx.writer.write_string_set(self.sigs)
        ctx.writer.write_string(self.ca)

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


@dataclass(kw_only=True)
class ValidPathInfo(UnkeyedValidPathInfo):
    """Metadata for a store path (including the path)."""

    path: StorePath

    def __hash__(self) -> int:
        return hash(self.path)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ValidPathInfo):
            return False
        return self.path == other.path

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        path = await ctx.reader.read_string(StorePath)
        info = await UnkeyedValidPathInfo.deserialize(ctx)
        return info.with_path(path)  # type: ignore[return-value]

    def serialize(self, ctx: WriteContext) -> None:
        ctx.writer.write_string(self.path)
        super().serialize(ctx)

    def to_bytes(self) -> bytes:
        buf = wire.BytesWriter()
        self.serialize(WriteContext(writer=buf, version=0))
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

        # Use store path hash in the NAR URL so the cache can resolve it
        # without needing a DB-backed NAR hash index.
        path_hash = str(self.path).rsplit("/", 1)[-1].split("-", 1)[0]

        lines = [
            f"StorePath: {self.path}",
            f"URL: nar/{path_hash}.nar",
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
            lines.append(f"Deriver: {str(self.deriver).split('/')[-1]}")

        lines.extend(f"Sig: {sig}" for sig in sorted(self.sigs))

        if self.ca:
            lines.append(f"CA: {self.ca}")

        return "\n".join(lines) + "\n"


@dataclass
class SubstitutablePathInfo:
    """Metadata for a substitutable path (missing but available)."""

    deriver: StorePath
    references: StorePathSet
    download_size: int
    nar_size: int

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.deriver = await ctx.reader.read_string(StorePath)
        obj.references = await ctx.reader.read_string_set(StorePath)
        obj.download_size = await ctx.reader.read_uint64()
        obj.nar_size = await ctx.reader.read_uint64()
        return obj

    def serialize(self, ctx: WriteContext) -> None:
        ctx.writer.write_string(self.deriver)
        ctx.writer.write_string_set(self.references)
        ctx.writer.write_uint64(self.download_size)
        ctx.writer.write_uint64(self.nar_size)
