# Dynamic Derivation Modularization Completion

The modularization of `DynamicDerivationResolver` into `CaRealisationManager` and `UnknownOutputResolver` is architecturally complete, but requires final logical verification and cleanup.

## Tasks
- [ ] **Verify Trampoline Logic**: Ensure `UnknownOutputResolver.on_build_complete` correctly handles nested `DerivedPath`s and DAG re-injection.
- [ ] **Realisation Integration**: Fix the type mismatch in `CaRealisationManager` where `Realisation` (TypedDict) is passed where a raw `dict` is expected by internal ops.
- [ ] **Scheduler Hook Cleanup**: confirm all calls from `Scheduler.execute_build` to the new managers are robust against connection failures.
- [ ] **Test Coverage**: Run `pytest tests/functional/test_ca_ops.py` and `pytest tests/functional/test_pynixd_delegation_build.py` to ensure zero regressions in the new modular structure.
- [ ] **Obsolete Code**: Confirm `pynixd/dynamic_resolver.py` has been fully removed (it should be gone already).
