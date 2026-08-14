nanopynix_bindings.flake.__prefix__$:
    \from nanopynix_bindings.expr import EvalState, Value

nanopynix_bindings.flake.FlakeRef.to_attrs$:
    def to_attrs(self) -> dict[str, str | int | bool]: ...

nanopynix_bindings.flake.LockedFlake.find_input$:
    def find_input(self, path: list[str]) -> dict[str, object] | None:
        """Find one node of the lock graph, as InstallableFlake::nixpkgsFlakeRef does"""

nanopynix_bindings.flake.LockedFlake.write_lock_file$:
    def write_lock_file(self) -> None:
        """Write the in-memory lock file to the flake's flake.lock on disk"""

nanopynix_bindings.flake.lock_flake$:
    def lock_flake(
        state: EvalState,
        flake_ref: FlakeRef,
        update_inputs: bool | list[str] = False,
        write_lock_file: bool = True,
        flake_settings: dict[str, str] = {},
    ) -> LockedFlake:
        """Lock a flake reference, returning a LockedFlake"""

nanopynix_bindings.flake.get_flake$:
    def get_flake(state: EvalState, flake_ref: FlakeRef, use_registries: bool = True) -> FlakeRef:
        """Resolve a flake reference (without locking)"""

nanopynix_bindings.flake.metadata_json$:
    def metadata_json(state: EvalState, locked_flake: LockedFlake) -> str:
        """The JSON object that `nix flake metadata --json` prints"""

nanopynix_bindings.flake.call_flake$:
    def call_flake(state: EvalState, locked_flake: LockedFlake) -> Value:
        """Call a locked flake's outputs function, returning a Value"""

nanopynix_bindings.flake.eval_flake$:
    def eval_flake(
        state: EvalState, ref: str, write_lock_file: bool = True, flake_settings: dict[str, str] = {}
    ) -> Value:
        """Lock and evaluate a flake, returning its outputs as a Value"""

nanopynix_bindings.flake.parse_flake_ref$:
    def parse_flake_ref(url: str, fetch_settings: dict[str, str] = {}) -> FlakeRef:
        """Parse a flake reference string (e.g. 'github:NixOS/nixpkgs')"""
