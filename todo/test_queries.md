# TODO: Test Advanced Store Queries

Implement functional tests for "plumbing" query operations used by advanced Nix commands.

## Operations to Test
*   **`QueryReferrers`**: Used by `nix why-depends` to find what depends on a path.
*   **`QueryPathFromHashPart`**: Used when only a hash prefix is available.
*   **`QueryValidDerivers`**: Used to find the `.drv` producing a store path.
*   **`QueryMissing`**: Used by Nix to estimate what needs to be built/fetched.
*   **`FindRoots`**: Verify that GC roots are correctly reported (even if currently no-op).
*   **`AddBuildLog`**: Verify that build logs can be retrieved via `nix log`.

## Verification Criteria
- [ ] Queries return accurate data from the backend.
- [ ] Large result sets (e.g., many referrers) are handled correctly.
- [ ] `QueryMissing` correctly accounts for paths available in `pynixd`'s cache vs the backend.
