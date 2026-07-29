"""Typed models for every Nix store type.

Nix configures a store through its URI. The scheme picks the implementation,
and the query parameters set that implementation's settings::

    local://?root=/tmp/x&require-sigs=false

This module gives each store type a Pydantic model, so the same configuration
can be written as Python with names, types and validation::

    stores.Local(root="/tmp/x", require_sigs=False)

:func:`parse` turns a URI into a model, and :meth:`StoreConfig.uri` turns a
model back into a URI. Both go through Nix's own ``StoreReference``, so the
strings this module produces are the strings Nix produces.

**A store setting is not a global setting.** Nix registers only three of these
names in ``globalConfig`` -- ``require-sigs``, ``build-dir`` and
``system-features``. The rest exist per store and nowhere else, so a URI
parameter is the only way to set them. This was measured: a store opened with
``require-sigs=false`` in its URI accepted an unsigned path while the global
``require-sigs`` was still true.

The class hierarchy mirrors Nix's own C++ configuration classes, and
:func:`check_all_store_model_drift` compares each model against the store
registry of the Nix that nanopynix is linked with.
"""

from __future__ import annotations

import json
from types import UnionType
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Union, cast, get_args, get_origin
from urllib.parse import quote

from nanopynix_bindings import store as nanopynix_store
from pydantic import AliasChoices, Field, model_validator

from nanopynix._typechecking import BEARTYPING
from nanopynix.settings import (
    NIX_2_34,
    NIX_2_35,
    NixConfigModel,
    NixStoreDefaults,
    SettingsDrift,
    field_is_supported,
    field_key,
    fill_unset_fields,
    since,
    until,
)

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Iterator, Mapping


#: Marks a field that names part of the URI itself rather than a query
#: parameter. Rides in ``json_schema_extra``, the same slot the settings
#: models use for ``nix_version_min`` and ``mutability``.
URI_PART_KEY = "uri_part"

type UriPart = Literal["authority", "scheme"]

#: Characters left unescaped in a query parameter value. A store setting often
#: holds a path or a nested store URI, and escaping ``/`` or ``:`` makes those
#: unreadable for no gain -- Nix parses them either way.
_QUERY_SAFE = "/:"


def _authority() -> Any:
    """A required field holding the URI authority, such as ``user@host``."""
    return Field(json_schema_extra={URI_PART_KEY: "authority"})


def _optional_authority() -> Any:
    """An optional field holding the URI authority."""
    return Field(default=None, json_schema_extra={URI_PART_KEY: "authority"})


def _url_scheme() -> Any:
    """An optional field choosing between the schemes one store type answers to."""
    return Field(default=None, json_schema_extra={URI_PART_KEY: "scheme"})


def _uri_part_of(field: Any) -> UriPart | None:
    extras: dict[str, Any] = field.json_schema_extra or {}
    value: Any = extras.get(URI_PART_KEY)
    return value if value in {"authority", "scheme"} else None


class StoreConfig(NixConfigModel):
    """The settings every Nix store type accepts, and the base of all the rest.

    These six are the members of Nix's own ``StoreConfig``, so every store type
    in :func:`list_store_types` registers all of them.

    ``store_dir`` is spelled ``store`` in the URI. That is a name Nix uses
    twice: here it is the *logical location of the store*, and in
    ``globalConfig`` it is the *URL of the store to use*. The two are unrelated,
    which is why the global one keeps the plain name on
    :class:`~nanopynix.settings.NixGlobalSettings` and this one is renamed.
    Two paths can only be copied between two stores that agree on it.
    """

    #: The URI scheme this store type renders. Subclasses set it.
    store_scheme: ClassVar[str]
    #: The key this store type has in Nix's store registry, or ``None`` when
    #: Nix has no registry entry for it. Only :class:`Auto` lacks one, because
    #: ``openStore`` resolves ``auto`` to another type rather than implementing
    #: a store itself. :func:`check_store_model_drift` uses this name.
    store_type_name: ClassVar[str | None] = None

    # Two aliases rather than one, so both spellings read and only one writes.
    # A plain `alias="store"` would make `store` the *only* keyword a type
    # checker accepts, and `stores.Local(store_dir=...)` a reported error.
    store_dir: str | None = Field(
        default=None,
        validation_alias=AliasChoices("store", "store_dir"),
        serialization_alias="store",
    )
    path_info_cache_size: int | None = None
    priority: int | None = None
    system_features: list[str] | None = None
    trusted: bool | None = None
    want_mass_query: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _split_list_values(cls, data: Any) -> Any:
        """Split a whitespace-joined string into a list, for list-typed fields.

        Every value in a URI is a string, and Nix joins a list setting with
        spaces. This is the inverse, so :func:`parse` and :meth:`uri` round-trip
        a list setting such as ``system-features``.
        """
        if not isinstance(data, dict):
            return data
        typed = cast("dict[Any, Any]", data)
        list_keys: set[str] = set()
        for name, field in cls.model_fields.items():
            if _annotation_is_list(field.annotation):
                # Both spellings, because populate_by_name accepts both.
                list_keys.add(name)
                list_keys.add(field_key(name, field))
        return {
            key: value.split() if key in list_keys and isinstance(value, str) else value for key, value in typed.items()
        }

    def params(self) -> dict[str, str]:
        """The URI query parameters this configuration renders to.

        The fields that name part of the URI itself -- the authority and, where
        a type answers to more than one, the scheme -- are not parameters and
        are excluded.
        """
        excluded = {
            field_key(name, field) for name, field in type(self).model_fields.items() if _uri_part_of(field) is not None
        }
        return {key: value for key, value in self._iter_set() if key not in excluded}

    def uri(self) -> str:
        """Render this configuration as a store URI.

        The result is normalised by Nix itself, so it is exactly what Nix would
        write for the same store. Parameters come out in Nix's order rather than
        the order they were set.
        """
        return nanopynix_store.render_store_reference(self._render_raw())

    def _render_raw(self) -> str:
        query = "&".join(f"{key}={quote(value, safe=_QUERY_SAFE)}" for key, value in sorted(self.params().items()))
        base = self._base()
        return f"{base}?{query}" if query else base

    def _base(self) -> str:
        """Everything before the query: the scheme, and the authority after it."""
        scheme = self._uri_field("scheme") or type(self).store_scheme
        return f"{scheme}://{self._uri_field('authority') or ''}"

    def _uri_field(self, part: UriPart) -> str | None:
        """The value of the field that names ``part`` of the URI, if it is set."""
        for name, field in type(self).model_fields.items():
            if _uri_part_of(field) == part:
                value: Any = getattr(self, name)
                return None if value is None else str(value)
        return None

    def __str__(self) -> str:
        return self.uri()


def _annotation_is_list(annotation: Any) -> bool:
    """True when ``annotation`` is ``list[...]``, or a union that contains one."""
    origin = get_origin(annotation)
    if origin is list:
        return True
    if origin in {UnionType, Union}:
        return any(_annotation_is_list(argument) for argument in get_args(annotation))
    return False


class _LocalFS(StoreConfig):
    """The layout of a store that lives in the local file system.

    Nix calls this ``LocalFSStoreConfig``. ``root`` prefixes the other three,
    which is how a store is relocated into a directory of its own.

    Note that a relocated store's paths are not executable where they claim to
    be: a binary built for ``/nix/store/...`` still says so, whatever ``root``
    puts in front of it. See :mod:`nanopynix.store_exec`.
    """

    root: str | None = None
    state: str | None = None
    log: str | None = None
    real: str | None = None


class _Remote(StoreConfig):
    """The connection pool of a store reached over a socket or over SSH."""

    max_connections: int | None = None
    max_connection_age: int | None = None


class _CommonSSH(StoreConfig):
    """What every SSH-backed store needs to reach the remote machine."""

    ssh_key: str | None = None
    base64_ssh_public_host_key: str | None = None
    compress: bool | None = None
    remote_store: str | None = None
    remote_program: list[str] | None = None


class _BinaryCache(StoreConfig):
    """What every binary cache does to the NARs it stores."""

    compression: str | None = None
    compression_level: int | None = None
    index_debug_info: bool | None = None
    local_nar_cache: str | None = None
    parallel_compression: bool | None = None
    secret_key: str | None = None
    secret_keys: str | None = None
    write_nar_listing: bool | None = None


class _RemoteCache(_BinaryCache):
    """A binary cache reached over the network, and so through curl and TLS.

    Nix 2.31 has neither of these two. It gained the TLS client certificate in
    2.34 and the retry policy in 2.35.

    The per-file compression settings are deliberately not here. They belong to
    both subclasses, but not since the same version: S3 has had them all along,
    and HTTP gained them in 2.34, so each states its own gate.
    """

    tls_certificate: str | None = since(NIX_2_34)
    tls_private_key: str | None = since(NIX_2_34)
    retry_attempts: int | None = since(NIX_2_35)
    retry_delay: int | None = since(NIX_2_35)
    retry_delay_rate_limited: int | None = since(NIX_2_35)
    retry_max_delay: int | None = since(NIX_2_35)


class Dummy(StoreConfig):
    """A store that holds nothing. Useful to evaluate without a real store."""

    store_scheme: ClassVar[str] = "dummy"
    store_type_name: ClassVar[str | None] = "Dummy Store"

    read_only: bool | None = since(NIX_2_34)


class Local(_LocalFS):
    """A store this process reads and writes directly, with no daemon.

    ``root`` relocates the whole store, which is what makes an isolated store
    for a test or a build possible.
    """

    store_scheme: ClassVar[str] = "local"
    store_type_name: ClassVar[str | None] = "Local Store"

    build_dir: str | None = None
    read_only: bool | None = None
    require_sigs: bool | None = None
    #: Both gated behind the ``local-overlay-store`` experimental feature, and
    #: both added in Nix 2.34.
    ignore_gc_delete_failure: bool | None = since(NIX_2_34)
    use_roots_daemon: bool | None = since(NIX_2_34)


class LocalOverlay(Local):
    """Two local stores stacked with OverlayFS: a read-only lower, a writable upper.

    Nix gates this type behind the ``local-overlay-store`` experimental
    feature, and refuses the URI without it.

    Every setting of :class:`Local` applies, because Nix derives this
    configuration from that one. The mount itself is not Nix's concern --
    see :class:`~nanopynix.namespace.OverlayNamespace`, which prepares the
    directories and the mount and then renders one of these.
    """

    store_scheme: ClassVar[str] = "local-overlay"
    store_type_name: ClassVar[str | None] = "Experimental Local Overlay Store"

    lower_store: str | None = None
    upper_layer: str | None = None
    check_mount: bool | None = None
    remount_hook: str | None = None


class Daemon(_LocalFS, _Remote):
    """A store reached through ``nix-daemon`` over a Unix socket.

    ``socket`` names the socket. Left unset, Nix uses the default socket of the
    host, which is what the bare ``daemon`` URI means.
    """

    store_scheme: ClassVar[str] = "unix"
    store_type_name: ClassVar[str | None] = "Local Daemon Store"

    socket: str | None = _optional_authority()


class Ssh(_CommonSSH):
    """A store on another machine, driven by ``nix-store`` over SSH.

    The older of the two SSH stores. It shells out to ``nix-store --serve``,
    so it can talk to a remote Nix that is much older than this one. It cannot
    build; use :class:`SshNg` for that.
    """

    store_scheme: ClassVar[str] = "ssh"
    store_type_name: ClassVar[str | None] = "SSH Store"

    host: str = _authority()
    max_connections: int | None = None
    log_fd: int | None = None


class SshNg(_CommonSSH, _Remote):
    """A store on another machine, driven by ``nix-daemon`` over SSH.

    It speaks the full daemon protocol, so unlike :class:`Ssh` it can build.
    """

    store_scheme: ClassVar[str] = "ssh-ng"
    store_type_name: ClassVar[str | None] = "Experimental SSH Store"

    host: str = _authority()


class MountedSshNg(SshNg, _LocalFS):
    """An :class:`SshNg` store whose store directory is also mounted locally.

    Nix gates this type behind the ``mounted-ssh-store`` experimental feature.
    The remote does the building, and the local mount means the results can be
    read without copying them.
    """

    store_scheme: ClassVar[str] = "mounted-ssh-ng"
    store_type_name: ClassVar[str | None] = "Experimental SSH Store with filesystem mounted"


class FileBinaryCache(_BinaryCache):
    """A binary cache in a local directory."""

    store_scheme: ClassVar[str] = "file"
    store_type_name: ClassVar[str | None] = "Local Binary Cache Store"

    path: str = _authority()


class HttpBinaryCache(_RemoteCache):
    """A binary cache served over HTTP, such as ``cache.nixos.org``.

    ``host`` holds everything between the scheme and the query, so a cache
    under a sub-path is written ``host="example.com/nix"``.
    """

    store_scheme: ClassVar[str] = "https"
    store_type_name: ClassVar[str | None] = "HTTP Binary Cache Store"

    url_scheme: Literal["http", "https"] | None = _url_scheme()
    host: str = _authority()
    #: Added in Nix 2.34. The S3 cache has had the same three all along, and
    #: declares them without a gate.
    log_compression: str | None = since(NIX_2_34)
    ls_compression: str | None = since(NIX_2_34)
    narinfo_compression: str | None = since(NIX_2_34)


class S3BinaryCache(_RemoteCache):
    """A binary cache in an S3 bucket.

    ``scheme`` here chooses how *this store* talks to S3, and is a setting of
    the store. It is not the URI scheme, which is always ``s3``.
    """

    store_scheme: ClassVar[str] = "s3"
    store_type_name: ClassVar[str | None] = "S3 Binary Cache Store"

    bucket: str = _authority()
    #: Present on every supported Nix, unlike the HTTP cache's copies.
    log_compression: str | None = None
    ls_compression: str | None = None
    narinfo_compression: str | None = None
    region: str | None = None
    profile: str | None = None
    endpoint: str | None = None
    scheme: str | None = None
    multipart_upload: bool | None = None
    addressing_style: str | None = since(NIX_2_34)
    storage_class: str | None = since(NIX_2_34)
    multipart_threshold: int | None = since(NIX_2_34)
    #: Nix 2.34 renamed ``buffer-size`` to ``multipart-chunk-size`` and kept
    #: the old name as an alias. Before 2.34 only the old name exists, so both
    #: are here, each bounded to the versions that have it.
    multipart_chunk_size: int | None = since(NIX_2_34)
    buffer_size: int | None = until(NIX_2_34)


class Auto(StoreConfig):
    """Whatever store the host is configured to use.

    This is the default, and what the URI ``auto`` means. Nix picks the daemon
    when a daemon socket is there and this user cannot write the store
    directly, and a local store otherwise.

    It carries only the settings every store type accepts, because which type
    it resolves to is not known until it is opened. To set anything else, name
    the store type.
    """

    store_scheme: ClassVar[str] = "auto"
    store_type_name: ClassVar[str | None] = None

    def _base(self) -> str:
        # "auto" is not a scheme and has no authority, so it has no "://".
        return "auto"


#: Every concrete store model, in the order they are documented.
STORE_MODELS: tuple[type[StoreConfig], ...] = (
    Auto,
    Dummy,
    Local,
    LocalOverlay,
    Daemon,
    Ssh,
    SshNg,
    MountedSshNg,
    FileBinaryCache,
    HttpBinaryCache,
    S3BinaryCache,
)

#: URI scheme to model. ``http`` and ``https`` share one model, which keeps the
#: scheme it was parsed from in ``url_scheme``.
_BY_SCHEME: dict[str, type[StoreConfig]] = {
    "dummy": Dummy,
    "local": Local,
    "local-overlay": LocalOverlay,
    "unix": Daemon,
    "ssh": Ssh,
    "ssh-ng": SshNg,
    "mounted-ssh-ng": MountedSshNg,
    "file": FileBinaryCache,
    "http": HttpBinaryCache,
    "https": HttpBinaryCache,
    "s3": S3BinaryCache,
}


def parse(uri: str) -> StoreConfig:
    """Parse a store URI into the model of its store type.

    Nix's own ``StoreReference::parse`` splits the URI, so anything Nix accepts
    is accepted here. The parameters are then validated against the model,
    which is stricter than Nix: a parameter the store type does not have is an
    error rather than something Nix ignores.

    Args:
        uri: A store URI, such as ``local://?root=/tmp/x`` or ``daemon``.

    Returns:
        The model for the store type the URI names.

    Raises:
        ValueError: The scheme has no model, or a parameter is not a setting of
            that store type.
    """
    reference = cast("dict[str, Any]", nanopynix_store.parse_store_reference(uri))  # type: ignore[reportUnknownArgumentType] -- C++ extension without type stubs
    params: dict[str, str] = dict(reference["params"])
    kind: str = reference["type"]
    if kind == "Auto":
        return _validate(Auto, params, uri)
    scheme: str = reference["scheme"]
    model = _BY_SCHEME.get(scheme)
    if model is None:
        known = ", ".join(sorted(_BY_SCHEME))
        raise ValueError(f"no store model for scheme {scheme!r} in {uri!r}; known schemes: {known}")
    data: dict[str, Any] = dict(params)
    authority: str = reference["authority"]
    for name, field in model.model_fields.items():
        part = _uri_part_of(field)
        if part == "authority" and (authority or field.is_required()):
            # An empty authority on an optional field is the absence of one,
            # not the empty string, so the model round-trips equal to itself.
            data[name] = authority
        elif part == "scheme" and scheme != model.store_scheme:
            # Only when it is not the type's own scheme: leaving it unset is
            # what renders that scheme back.
            data[name] = scheme
    return _validate(model, data, uri)


def _validate(model: type[StoreConfig], data: Mapping[str, Any], uri: str) -> StoreConfig:
    try:
        return model.model_validate(dict(data))
    except ValueError as error:
        raise ValueError(f"cannot read {uri!r} as {model.__name__}: {error}") from error


def resolve_store_spec(spec: StoreConfig | str, defaults: NixStoreDefaults | None = None) -> str:
    """Render a store spec to a URI, filling unset fields from session defaults.

    This is what makes a session-wide store default real. Nix has no global for
    ``priority``, ``trusted``, ``want-mass-query`` or ``path-info-cache-size``,
    so the only place they can go is the URI of each store, and this is what
    puts them there. A value set on the store itself always wins.

    A string spec passes through untouched. Merging a default into it would
    mean reparsing and rewriting a URI the caller wrote by hand, and this
    library does not rewrite what it was given -- pass a model to get defaults.

    Args:
        spec: A store model, or a URI string.
        defaults: The session-wide store defaults, or ``None`` for no defaults.

    Returns:
        The URI to open.
    """
    if isinstance(spec, str):
        return spec
    if defaults is None:
        return spec.uri()
    return fill_unset_fields(spec, defaults).uri()


def list_store_types() -> dict[str, dict[str, Any]]:
    """Return Nix's live registry of store types, keyed by type name.

    The registry is built by one static initialiser per linked store
    implementation, so this reports what this build can open rather than what
    Nix documents. Each entry has ``doc``, ``uri-schemes``, ``settings`` and
    ``experimentalFeature``.
    """
    raw: dict[str, dict[str, Any]] = json.loads(nanopynix_store.list_store_types_json())
    return raw


def check_store_model_drift(
    model: type[StoreConfig],
    registry: Mapping[str, Any] | None = None,
) -> SettingsDrift:
    """Compare one store model against the settings Nix's registry lists for it.

    Args:
        model: The store model to check. :class:`Auto` has no registry entry,
            so it is reported as having no drift.
        registry: The registry to compare against; defaults to the live one.

    Returns:
        Settings Nix registers for this store type but the model lacks, and
        fields the model has that Nix does not register (excluding aliases and
        the fields that name part of the URI).
    """
    type_name = model.store_type_name
    if type_name is None:
        return SettingsDrift(missing=[], extra=[])
    if registry is None:
        registry = list_store_types()
    entry: Any = registry.get(type_name)
    if entry is None:
        raise ValueError(f"Nix does not register a store type named {type_name!r}")
    settings: dict[str, Any] = entry["settings"]
    known = set(settings)
    aliases = {alias for setting in settings.values() for alias in setting.get("aliases", [])}
    fields = set(_param_keys(model))
    return SettingsDrift(missing=sorted(known - fields), extra=sorted(fields - known - aliases))


def check_all_store_model_drift() -> dict[str, SettingsDrift]:
    """Run :func:`check_store_model_drift` over every model, keyed by class name."""
    registry = list_store_types()
    return {model.__name__: check_store_model_drift(model, registry) for model in STORE_MODELS}


def _param_keys(model: type[StoreConfig]) -> Iterator[str]:
    # Version-gated fields are skipped on a Nix that does not have them, the
    # same way the settings surfaces do it. That is what lets one model serve
    # every supported Nix and still be checked field for field against each.
    for name, field in model.model_fields.items():
        if _uri_part_of(field) is None and field_is_supported(field):
            yield field_key(name, field)
