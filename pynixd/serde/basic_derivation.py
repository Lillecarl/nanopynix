from __future__ import annotations

import json

from ..store_path import StorePath as DomainStorePath
from ..system_features import PYNIXD_HANDLED_FEATURES
from .derivation_output import DerivationOutput  # noqa: TC001
from .store_path import StorePath  # noqa: TC001
from .wire_message import WireField
from .wire_message import WireModel


class BasicDerivation(WireModel):
    """Wire mirror of BasicDerivation."""

    outputs: dict[str, DerivationOutput]
    input_srcs: set[StorePath]
    platform: str
    builder: str
    args: list[str]
    env: dict[str, str]
    is_dynamic: bool = WireField(default=False, serialize=False, deserialize=False)

    @property
    def requires_nix(self) -> bool:
        return not self.supports_lix()

    @property
    def build_local(self) -> bool:
        return self.env.get("pynixd_fast") == "1" or self.env.get("preferLocalBuild") == "1"

    @property
    def required_system_features(self) -> set[str]:
        raw = self.env.get("requiredSystemFeatures", "")
        if not raw:
            return set()
        return set(raw.split())

    @property
    def effective_required_features(self) -> set[str]:
        return self.required_system_features - PYNIXD_HANDLED_FEATURES

    def supports_lix(self) -> bool:
        if self.is_dynamic:
            return False
        return all(not (output.is_ca or output.is_deferred) for output in self.outputs.values())

    def output_paths(self) -> dict[str, DomainStorePath]:
        return {name: DomainStorePath(output.path) for name, output in self.outputs.items()}

    def to_stats_json(self) -> str:
        return json.dumps(
            {
                "builder": self.builder,
                "outputs": list(self.outputs.keys()),
                "system": self.env.get("system", self.platform),
            },
            sort_keys=True,
        )

    @property
    def has_dynamic_outputs(self) -> bool:
        return any(output.is_dynamic_output for output in self.outputs.values())
