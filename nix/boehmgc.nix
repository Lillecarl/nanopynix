# The collector that every build with `gc = true` links, patched.
#
# **The patch corrects an abort that kills the process, and it has nothing to
# do with a sanitizer.** Boehm stops the world by signalling every registered
# thread. A thread that exits between the moment Boehm reads it out of
# `GC_threads` and the moment `pthread_kill` reaches it is undefined by POSIX,
# and this glibc answers `EINVAL` rather than the `ESRCH` that Boehm already
# tolerates. Boehm then calls `abort`:
#
#   pthread_kill failed at suspend: errcode= 22
#
# `./patches/boehmgc-tolerate-suspend-thread-exit-race.patch` widens the
# tolerance to `EINVAL` at both stop-the-world signal sites. Read that file:
# it carries the reason `EINVAL` cannot mean "bad signal number" here.
#
# **It lived in `sanitizer.nix` until now, and so reached the sanitized builds
# alone.** ThreadSanitizer is where the race was first seen, because it slows
# the process enough to make the race land, and that made it look like a
# property of the instrumented build. It is not. The concurrency soak
# reproduces the same abort in a plain dev shell in 6 of 15 runs, every time
# straight after
# `test_an_operation_that_runs_a_nix_thread_pool_still_completes` -- a test
# whose whole subject is a pool of Nix threads that start and exit.
#
# So the collector that ships was the one collector with the defect in it.
#
# **The second patch makes the log of the collector readable.**
# `./patches/boehmgc-log-file-pid.patch` expands the first `%d` of
# `GC_LOG_FILE` to the process id. bdwgc writes no process id on any line, so
# the four processes of the short reproduction of #70 append to one file and
# every count in it is a sum. A name with no `%d` behaves exactly as before.
{ }:
{
  patchBoehmGC =
    boehmgc:
    boehmgc.overrideAttrs (old: {
      patches = (old.patches or [ ]) ++ [
        ./patches/boehmgc-tolerate-suspend-thread-exit-race.patch
        ./patches/boehmgc-log-file-pid.patch
      ];
    });
}
