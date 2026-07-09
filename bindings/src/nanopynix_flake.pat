nanopynix_flake.__prefix__:
    \from nanopynix_expr import EvalState, Value

nanopynix_flake.FlakeRef.__init__:
    def __init__(self, ref: object) -> None: ...

nanopynix_flake.FlakeRef.to_attrs:
    def to_attrs(self) -> dict[str, str | int | bool]: ...

nanopynix_flake.LockedFlake.inputs:
    def inputs(self) -> dict[str, object]: ...

nanopynix_flake.LockedFlake.write_lock_file:
    def write_lock_file(self) -> None:
        """Write the in-memory lock file to the flake's flake.lock on disk"""

nanopynix_flake.lock_flake:
    def lock_flake(
        state: EvalState,
        flake_ref: FlakeRef,
        update_all: bool = False,
        update_inputs: list[str] = [],
        write_lock_file: bool = True,
    ) -> LockedFlake:
        """
        Lock a flake reference, returning a LockedFlake with description and inputs
        """

nanopynix_flake.get_flake:
    def get_flake(state: EvalState, flake_ref: FlakeRef, use_registries: bool = True) -> FlakeRef:
        """Resolve a flake reference (without locking)"""

nanopynix_flake.call_flake:
    def call_flake(state: EvalState, locked_flake: LockedFlake) -> Value:
        """Call a locked flake's outputs function, returning a Value"""

nanopynix_flake.eval_flake:
    def eval_flake(state: EvalState, ref: str, write_lock_file: bool = True) -> Value:
        """Lock and evaluate a flake, returning its outputs as a Value"""
