from __future__ import annotations

from nanopynix_proto.nix.common import LogLevel

type LogLevelInput = LogLevel | int | str

_LOG_LEVEL_NAMES: dict[str, LogLevel] = {
    "error": LogLevel.ERROR,
    "err": LogLevel.ERROR,
    "warn": LogLevel.WARN,
    "warning": LogLevel.WARN,
    "notice": LogLevel.NOTICE,
    "info": LogLevel.INFO,
    "talkative": LogLevel.TALKATIVE,
    "chatty": LogLevel.CHATTY,
    "debug": LogLevel.DEBUG,
    "vomit": LogLevel.VOMIT,
}


def normalize_log_level(value: LogLevelInput) -> LogLevel:
    if isinstance(value, LogLevel):
        return value
    if isinstance(value, bool):
        raise TypeError("verbosity must be a log level name or integer from 0 through 7")
    if isinstance(value, int):
        return _log_level_from_int(value)

    raw = value.strip()
    if raw == "":
        raise ValueError("verbosity cannot be empty")
    if raw.isdecimal():
        return _log_level_from_int(int(raw))

    key = raw.replace("-", "_").lower()
    if key.startswith("lvl_"):
        key = key[4:]
    elif key.startswith("lvl"):
        key = key[3:]
    key = key.removeprefix("log_level_")

    try:
        return _LOG_LEVEL_NAMES[key]
    except KeyError as exc:
        valid = ", ".join(sorted(_LOG_LEVEL_NAMES))
        raise ValueError(f"unknown verbosity {value!r}; expected 0-7 or one of: {valid}") from exc


def _log_level_from_int(value: int) -> LogLevel:
    if value < int(LogLevel.ERROR) or value > int(LogLevel.VOMIT):
        raise ValueError(f"verbosity must be between 0 and 7, got {value}")
    return LogLevel(value)
