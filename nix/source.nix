/*
  The repository tree, filtered, for a derivation that needs the whole thing.

  **A bare `../.` copies the working directory and not the repository.** Under
  a flake evaluation the source is git-filtered and the difference does not
  show. Under `--file .`, which is how CI now builds every step, the copy took
  everything: 230 MiB against 5.7 MiB of tracked content. `.pytest-agent` was
  92 MiB of that, and it changes on every local pytest run, so each build
  minted a new 230 MiB store path. That is a full disk in a day.

  `lib.cleanSource` removes `.git` (12 MiB), `.jj` (73 MiB), `result` symlinks
  and editor droppings. It does not know about the caches below, which hold
  the rest of the weight. No tracked file matches its `.o`/`.so`/backup rules.

  **This is not `_SKIP_DIRS` from `tests/support/suppressions.py`.** That set
  answers "which directory holds no first-party source", and it lists `tmp`.
  This one answers "which directory does no derivation need", and `tmp` is
  tracked (`tmp/fs_closure.py`). Keep the two lists apart.

  A denylist, and not a list of the directories to keep: `.github` must
  survive, because the test runner reads the rendered workflows
  (`tests/meta/test_ci_step_policy.py`), and so must `.assets`. Those are the
  only two tracked directories whose name starts with a period, so a rule over
  the name alone cannot separate them from the caches.

  Removing `.git` also corrects a local failure: libgit2 refuses a root-owned
  repository, so `pynix/tests/test_real_store_scenario.py` failed whenever a
  colocated `.git` travelled into the store copy.
*/
{ lib }:
let
  skipDirs = [
    ".bench-dumps"
    ".direnv"
    ".hypothesis"
    ".pytest-agent"
    ".pytest_cache"
    ".ruff_cache"
    ".terraform"
    ".test-runs"
    "__pycache__"
    "node_modules"
  ];

  skipFiles = [
    "coverage.xml"
    "junit.xml"
    "gc-thread-debug.log"
    "test-gdb-output.log"
    "nanopynix-test-store-paths.txt"
  ];
in
lib.cleanSourceWith {
  name = "nanopynix-source";
  src = lib.cleanSource ../.;
  filter =
    path: type:
    let
      base = baseNameOf path;
    in
    !(
      (type == "directory" && builtins.elem base skipDirs)
      # `.coverage` is a file, and a local run writes a new one every time.
      || base == ".coverage"
      || lib.hasPrefix ".coverage." base
      # The reports and diagnostics of a run, written into the repository
      # root. `.gitignore` covers them too. Here as well, because this filter
      # is what keeps a local working copy out of the store: a developer who
      # ran the suite would otherwise rebuild every derivation that reads this
      # source, for a file that no build reads.
      || builtins.elem base skipFiles
    );
}
