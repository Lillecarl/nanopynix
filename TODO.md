# Test-store migration

- Replace the session-scoped default `store` and `eval_state` fixtures in
  `tests/conftest.py`.  The L1 binding, expression, flake, fetcher, primop,
  and extraction suites must instead use reusable, explicitly selected local
  and native-daemon Stores.
- Seed every Store-backed test path inside its selected private Store.  Do not
  query the host Store or derive fixtures from the `nix` executable's Store
  path.
- Keep `StorePathRecorder` for the multithreaded in-process build stress suite
  until a replacement is proven.  The proof must build paths in both private
  Store backends, close the owning Store/daemon connection, and show fixture
  teardown removes the Store roots without a temproot race.  Only then may the
  post-pytest deletion hook be removed or redesigned.
- Add one CI-selectable aggregate test target for the migrated dual-backend
  suites, so local and daemon coverage is validated together rather than only
  through individual module runs.
