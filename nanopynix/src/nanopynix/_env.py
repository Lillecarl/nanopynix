"""What a session may put in the environment of the process that runs Nix.

A session carries ``env`` to the process where Nix runs, and applies it before
Nix initialises. That serves every name Nix reads *after* ``initLibStore``:
``NIX_SSHOPTS``, which an ``ssh-ng://`` store reads when it opens the
connection, the proxy variables, and ``TMPDIR``.

**It cannot serve a name Nix reads while ``libnixstore`` loads.** Those reads
happen when the bindings are imported, which is before a session exists in
either engine: the forkserver preloads ``nanopynix.rpc.worker._worker``, and an
inproc caller imported ``nanopynix`` to construct the session at all. This
module refuses each such name, rather than accept a value that would do
nothing. The failure is silent otherwise, and a silent failure is what makes
this a correctness rule and not a preference.

**The refusal set is the union over the supported versions of Nix, and it does
not change with the version that the environment linked.** ``globals.cc``
moves a read from the constructor of the namespace-scope ``Settings`` object to
a function with a local static from one version to the next, so a per-version
set would give one caller two answers for one program. ``NIX_USER_CONF_FILES``
is the example that is already measured: Nix 2.31 reads it at static
initialisation, and 2.34 reads it in ``loadConfFile``. See
``nanopynix_testing.nix_markers``.

Every refused name has a route that works, and each message names it.
"""

from __future__ import annotations

# At the top level rather than under `TYPE_CHECKING`, because
# `validate_session_env` uses it in an `isinstance` guard for an untyped
# caller, and not only as an annotation.
from collections.abc import Mapping

from nanopynix._typechecking import no_runtime_type_check

OWNED_BY_NANOPYNIX = {
    "NIX_USER_CONF_FILES": "pass nix_conf=Path(...) to the session",
    "NIX_CONFIG": "pass settings=NixSettings(...) to the session",
    "NIX_REMOTE": "pass store_uri=... to the session",
    "NIX_PATH": "pass nix_path=... to the session",
}
"""Names a session parameter already owns, and the parameter that owns each one.

A session that carried one of these would contradict its own configuration.
``NIX_CONFIG`` is the least obvious member, and it is the clearest: a session
with ``load_config=False`` never calls ``loadConfFile``, which is the only code
that reads the variable, so the value would vanish for exactly the caller who
asked for the most control.
"""

READ_WHILE_LIBSTORE_LOADS = {
    "NIX_STORE_DIR": "name the directory with stores.Local(store_dir=...)",
    "NIX_STORE": "name the directory with stores.Local(store_dir=...)",
    "NIX_STATE_DIR": "name the directory with stores.Local(state=...)",
    "NIX_LOG_DIR": "name the directory with stores.Local(log=...)",
    "NIX_DAEMON_SOCKET_PATH": "name the socket with stores.Daemon(socket=...)",
    "NIX_SSL_CERT_FILE": "pass settings=NixSettings(ssl_cert_file=...) to the session",
    "SSL_CERT_FILE": "pass settings=NixSettings(ssl_cert_file=...) to the session",
    "NIX_REMOTE_SYSTEMS": "pass settings=NixSettings(builders=[...]) to the session",
    "NIX_DATA_DIR": "set it in the environment of this process before it imports nanopynix",
    "NIX_CONF_DIR": "set it in the environment of this process before it imports nanopynix",
    "NIX_IGNORE_SYMLINK_STORE": "set it in the environment of this process before it imports nanopynix",
}
"""Names Nix reads while ``libnixstore`` loads, and the route that works instead.

Read in ``src/libstore/globals.cc`` of Nix 2.31, 2.34 and 2.35. The first eight
have a first-class route, because Nix itself carries them as a store parameter
or as a setting. The last three name the installation rather than the session,
and the only place to set one is the environment of the whole process.
"""


@no_runtime_type_check  # this function *is* the runtime check, and it documents
# the exception type it raises. beartype's own check runs first otherwise, and
# an untyped caller gets a BeartypeCallHintParamViolation, which subclasses
# neither TypeError nor ValueError -- and gets nothing at all in a build with
# beartype off.
def validate_session_env(env: Mapping[str, str] | None) -> dict[str, str]:
    """Check an ``env`` mapping, and return it as a plain ``dict``.

    ``None`` gives an empty mapping. The result merges over the environment the
    process running Nix already has, and a name in the mapping wins. **A
    mapping cannot remove a name.** ``map<string, string>`` on the wire carries
    no absence, and a second field is more surface than the case justifies.

    Raises:
        TypeError: ``env`` is not a mapping of ``str`` to ``str``.
        ValueError: a name is empty, holds ``=`` or NUL, or a value holds NUL;
            or a name is one this module refuses. The message names the route
            that works.
    """
    if env is None:
        return {}
    if not isinstance(env, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance] -- runtime guard for untyped callers
        raise TypeError("env must be a mapping of str to str, or None")
    checked: dict[str, str] = {}
    for name, value in env.items():
        if not isinstance(name, str) or not isinstance(value, str):  # pyright: ignore[reportUnnecessaryIsInstance] -- runtime guard for untyped callers
            raise TypeError(f"env must be a mapping of str to str; {name!r} maps {type(name)} to {type(value)}")
        if not name:
            raise ValueError("env holds an empty name")
        # `putenv` takes `name=value`, so a name holding `=` names something
        # other than what it says, and a NUL truncates either half.
        if "=" in name or "\0" in name:
            raise ValueError(f"env name {name!r} holds '=' or a NUL, and cannot name an environment variable")
        if "\0" in value:
            raise ValueError(f"the value of env name {name!r} holds a NUL")
        owner = OWNED_BY_NANOPYNIX.get(name)
        if owner is not None:
            raise ValueError(
                f"{name} is a name nanopynix owns, so a session that carried it would contradict "
                f"its own configuration. Instead, {owner}."
            )
        route = READ_WHILE_LIBSTORE_LOADS.get(name)
        if route is not None:
            raise ValueError(
                f"{name} cannot take effect in a session: Nix reads it while libnixstore loads, "
                f"which is before any session exists. Instead, {route}."
            )
        checked[name] = value
    return checked
