"""Stable goal identity keys."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EnsureDerivedPathKey:
    derived_path: str
    substituter_fingerprint: str


@dataclass(frozen=True)
class BuildDerivationKey:
    drv_path: str
    derivation_fingerprint: str


@dataclass(frozen=True)
class SubstitutePathKey:
    path: str
    substituter_fingerprint: str
