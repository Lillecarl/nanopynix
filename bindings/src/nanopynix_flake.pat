nanopynix_flake.__prefix__:
    from nanopynix_expr import EvalState

nanopynix_flake.FlakeRef.__init__:
    def __init__(self, ref: "nix::FlakeRef") -> None: ...

nanopynix_flake.lock_flake:
    def lock_flake(state: EvalState, flake_ref: FlakeRef, update_lock_file: bool = True, write_lock_file: bool = True) -> LockedFlake:
        """
        Lock a flake reference, returning a LockedFlake with description and inputs
        """

nanopynix_flake.get_flake:
    def get_flake(state: EvalState, flake_ref: FlakeRef, use_registries: bool = True) -> FlakeRef:
        """Resolve a flake reference (without locking)"""
