# TODO: Test Content-Addressed (CA) Operations

Implement functional tests for Content-Addressed Nix protocol extensions.

## Operations to Test
*   **`QueryDerivationOutputMap`**: Resolve output names to CA paths.
*   **`QueryDerivationOutputsBatch`**: Batch version of the above.
*   **CA Build Execution**:
    *   Verify that CA derivations are built correctly by the scheduler.
    *   Verify that outputs are correctly pulled and cached by `pynixd`.

## Verification Criteria
- [ ] CA output mappings are correctly resolved and cached.
- [ ] Scheduler correctly handles the non-deterministic nature of CA outputs (until resolved).
- [ ] Integration with `nix3` CLI in CA mode.
