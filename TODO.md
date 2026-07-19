# Test-store migration

- [x] Replaced the session-scoped default `store` and `eval_state` fixtures in
  `tests/conftest.py`.  They now open `l1_nix_environment` (a sync,
  backend-parametrized counterpart of `shared_nix_environment` in
  `tests/support/nix_environment.py`), so the L1 binding, expression, flake,
  fetcher, primop, and extraction suites run against explicitly selected
  local and native-daemon Stores instead of the host default.
- [x] Seeded every Store-backed test path inside its selected private Store via
  the new `store_seeded_path` fixture (`Store.store_add_to_store`).  No test
  derives fixtures from the `nix` executable's own Store path anymore
  (`test_l1_store_bindings.py`'s `_nix_sp()` helper is gone).
- Keep `StorePathRecorder` for the multithreaded in-process build stress suite
  until a replacement is proven.  The proof must build paths in both private
  Store backends, close the owning Store/daemon connection, and show fixture
  teardown removes the Store roots without a temproot race.  Only then may the
  post-pytest deletion hook be removed or redesigned.
- Add one CI-selectable aggregate test target for the migrated dual-backend
  suites, so local and daemon coverage is validated together rather than only
  through individual module runs.
