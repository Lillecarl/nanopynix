/* nanopynix-store-exec -- run a program out of a relocated ("diverted") Nix
 * store, with that store visible at its own logical path.
 *
 * Why this exists
 * ---------------
 * A relocated store (`local://?root=/x`, `unix:///sock?root=/x`) reports
 * ordinary logical `/nix/store/...` paths while the bytes live under
 * `/x/nix/store/...`. Resolving the physical path and exec'ing *that* does not
 * work: the ELF interpreter, DT_RUNPATH, and every `/nix/store/...` string
 * baked into the binary and into the scripts it runs all still name the
 * logical directory. The only thing that makes such a closure runnable is
 * making the logical directory *be* the real store for the duration -- which
 * is exactly what `nix run` does (`chrootHelper` in nix's src/nix/run.cc).
 *
 * Why it is a separate binary rather than a binding
 * -------------------------------------------------
 * Four reasons, each sufficient on its own:
 *
 *   - `chrootHelper` lives in src/nix/run.cc, part of the `nix` CLI, in every
 *     version we build (checked 2.31, 2.34, git). It is in no libnix*
 *     component, so there is nothing to bind against.
 *   - Nix reaches it by re-exec'ing *its own binary* with argv[0] set to
 *     `__run_in_chroot`. From CPython, `getSelfExe()` is `python`, which has
 *     no such entry point.
 *   - `unshare(CLONE_NEWUSER)` fails with EINVAL in a multithreaded process,
 *     and CPython is multithreaded in practice. That is the very reason Nix
 *     re-execs instead of unsharing in place.
 *   - It ends in `execvp`: it replaces the process image rather than spawning
 *     a child, so it cannot be a function a library caller returns from.
 *
 * So the capability has to be a small exec-final helper, and it is written in
 * C with no dependencies so it stays trivially auditable -- this is code that
 * manipulates namespaces and mounts, and its whole security story is "it does
 * these few syscalls and nothing else".
 *
 * It is deliberately NOT setuid and must never be made so. It acquires its
 * mount privileges from an *unprivileged* user namespace, so the mounts it
 * makes are visible only inside the namespace it just created, and it drops
 * nothing and grants nothing that the calling user did not already have.
 *
 * Usage
 * -----
 *   nanopynix-store-exec <storeDir> <realStoreDir> <prog> [args...]
 *
 * `prog` is resolved through PATH *after* the store is in place, and becomes
 * argv[0] of the exec'd process. When storeDir and realStoreDir are the same
 * directory -- the ordinary, non-relocated case -- no namespace is created at
 * all and this is a bare `execvp`, so callers can route every exec through it
 * unconditionally instead of branching on the store layout.
 */

#define _GNU_SOURCE

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <sched.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#define PROG "nanopynix-store-exec"

/* Exit codes follow the shell's convention for exec failures, so a caller can
 * tell "the helper could not set up" (126) from "the program is not there"
 * (127) from whatever the program itself exits with. */
#define EXIT_SETUP_FAILED 126
#define EXIT_EXEC_FAILED 127

static void fail(const char *fmt, ...)
{
    int saved_errno = errno;
    va_list ap;

    fprintf(stderr, "%s: ", PROG);
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fprintf(stderr, ": %s\n", strerror(saved_errno));
    exit(EXIT_SETUP_FAILED);
}

static void fail_msg(const char *fmt, ...)
{
    va_list ap;

    fprintf(stderr, "%s: ", PROG);
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fprintf(stderr, "\n");
    exit(EXIT_SETUP_FAILED);
}

/* Write a whole string to a file, failing on anything short. Used only for the
 * /proc/self/{setgroups,uid_map,gid_map} triple, where a partial write is a
 * hard error rather than something to retry: the kernel accepts these in a
 * single write() or not at all. */
static void write_file(const char *path, const char *contents)
{
    ssize_t written;
    size_t len = strlen(contents);
    int fd = open(path, O_WRONLY | O_CLOEXEC);

    if (fd == -1)
        fail("opening %s", path);
    written = write(fd, contents, len);
    if (written == -1)
        fail("writing to %s", path);
    if ((size_t) written != len)
        fail_msg("short write to %s (%zd of %zu bytes)", path, written, len);
    if (close(fd) == -1)
        fail("closing %s", path);
}

static int path_exists(const char *path)
{
    struct stat st;

    return lstat(path, &st) == 0;
}

/* mkdir -p, in place, on a mutable copy of `path`. */
static void make_dirs(char *path, mode_t mode)
{
    char *slash = path;

    while ((slash = strchr(slash + 1, '/')) != NULL) {
        *slash = '\0';
        if (mkdir(path, mode) == -1 && errno != EEXIST)
            fail("creating directory %s", path);
        *slash = '/';
    }
    if (mkdir(path, mode) == -1 && errno != EEXIST)
        fail("creating directory %s", path);
}

static char *concat(const char *a, const char *b)
{
    size_t la = strlen(a);
    size_t lb = strlen(b);
    char *out = malloc(la + lb + 1);

    if (out == NULL)
        fail_msg("out of memory");
    memcpy(out, a, la);
    memcpy(out + la, b, lb);
    out[la + lb] = '\0';
    return out;
}

/* Strip trailing slashes so `/nix/store` and `/nix/store/` compare equal.
 * A lone "/" keeps its slash. */
static char *normalize(const char *path)
{
    char *out = strdup(path);
    size_t len;

    if (out == NULL)
        fail_msg("out of memory");
    len = strlen(out);
    while (len > 1 && out[len - 1] == '/')
        out[--len] = '\0';
    return out;
}

/* The `storeDir` mount point does not exist on this host, so there is nothing
 * to mount over. Build a throwaway root that contains it -- bind the real
 * store at the logical path inside a temp dir, then bind (or, for symlinks,
 * recreate) every top-level entry of `/` alongside it -- and chroot into that.
 *
 * This mirrors nix's own fallback. It is the uncommon path: it only triggers
 * on a host with no /nix at all. */
static void chroot_with_store(const char *store_dir, const char *real_store_dir)
{
    const char *tmp_root = getenv("TMPDIR");
    char *tmp_template;
    char *tmp_dir;
    char *mount_point;
    char *cwd;
    DIR *root;
    struct dirent *entry;

    /* Honour $TMPDIR like nix's own createTempDir does. A caller that set it
     * did so because /tmp is the wrong place (or not writable) here. */
    if (tmp_root == NULL || tmp_root[0] != '/')
        tmp_root = "/tmp";
    if (asprintf(&tmp_template, "%s/nanopynix-store-exec.XXXXXX", tmp_root) == -1)
        fail_msg("out of memory");

    tmp_dir = mkdtemp(tmp_template);
    if (tmp_dir == NULL)
        fail("creating a temporary directory under %s", tmp_root);

    mount_point = concat(tmp_dir, store_dir);
    make_dirs(mount_point, 0755);

    if (mount(real_store_dir, mount_point, "", MS_BIND | MS_REC, NULL) == -1)
        fail("bind-mounting %s on %s", real_store_dir, mount_point);

    root = opendir("/");
    if (root == NULL)
        fail("opening /");

    while ((entry = readdir(root)) != NULL) {
        char *src;
        char *dst;
        char *tmp_prefix;
        struct stat st;

        if (strcmp(entry->d_name, ".") == 0 || strcmp(entry->d_name, "..") == 0)
            continue;

        src = concat("/", entry->d_name);
        tmp_prefix = concat(tmp_dir, "/");
        dst = concat(tmp_prefix, entry->d_name);
        free(tmp_prefix);

        /* Skip anything the store mount point already created for us -- on a
         * typical layout that is `nix` itself. */
        if (path_exists(dst)) {
            free(src);
            free(dst);
            continue;
        }

        if (lstat(src, &st) == -1)
            fail("stat'ing %s", src);

        if (S_ISDIR(st.st_mode)) {
            if (mkdir(dst, 0700) == -1)
                fail("creating directory %s", dst);
            if (mount(src, dst, "", MS_BIND | MS_REC, NULL) == -1)
                fail("bind-mounting %s on %s", src, dst);
        } else if (S_ISLNK(st.st_mode)) {
            char target[PATH_MAX];
            ssize_t len = readlink(src, target, sizeof(target) - 1);

            if (len == -1)
                fail("reading symlink %s", src);
            target[len] = '\0';
            if (symlink(target, dst) == -1)
                fail("creating symlink %s", dst);
        }
        /* Regular files and devices directly under / are left out, exactly as
         * nix leaves them out: there is no sane way to bind a file onto a
         * path that does not exist yet, and nothing in a store closure
         * resolves through one. */

        free(src);
        free(dst);
    }
    closedir(root);

    /* Preserve the working directory across the chroot: the program we are
     * about to exec was invoked with a cwd that its caller chose, and every
     * relative path it is given is relative to that. */
    cwd = get_current_dir_name();
    if (cwd == NULL)
        fail("getting the current directory");

    if (chroot(tmp_dir) == -1)
        fail("chrooting into %s", tmp_dir);
    if (chdir(cwd) == -1)
        fail("changing back to %s inside the chroot", cwd);

    free(cwd);
    free(mount_point);
}

/* The mount point exists, so put the real store *at* it. Prefer overlayfs, so
 * that whatever the host store already has stays visible alongside the
 * relocated store's contents -- a plain bind would hide it, and a closure can
 * legitimately reference host paths that were substituted rather than copied.
 * Fall back to the bind if overlayfs is unavailable. */
static void mount_over_store(const char *store_dir, const char *real_store_dir)
{
    char *options;

    if (asprintf(&options, "lowerdir=%s:%s", store_dir, real_store_dir) == -1)
        fail_msg("out of memory");

    if (mount("overlay", store_dir, "overlay", 0, options) == -1
        && mount(real_store_dir, store_dir, "", MS_BIND | MS_REC, NULL) == -1)
        fail("mounting %s on %s", real_store_dir, store_dir);

    free(options);
}

int main(int argc, char **argv)
{
    char *store_dir;
    char *real_store_dir;
    char **args;
    int created_user_ns = 1;
    uid_t uid = getuid();
    gid_t gid = getgid();
    char id_map[64];

    if (argc < 4) {
        fprintf(stderr, "usage: %s <storeDir> <realStoreDir> <prog> [args...]\n", PROG);
        return EXIT_SETUP_FAILED;
    }

    store_dir = normalize(argv[1]);
    real_store_dir = normalize(argv[2]);
    args = &argv[3];

    /* Nothing is relocated, so there is nothing to set up. Staying out of the
     * way here is what lets callers use this unconditionally. */
    if (strcmp(store_dir, real_store_dir) == 0) {
        execvp(args[0], args);
        fprintf(stderr, "%s: executing %s: %s\n", PROG, args[0], strerror(errno));
        return EXIT_EXEC_FAILED;
    }

    if (store_dir[0] != '/' || real_store_dir[0] != '/')
        fail_msg("both storeDir and realStoreDir must be absolute (got %s and %s)", store_dir,
                 real_store_dir);

    /* A user namespace is what makes the mounts below possible without being
     * root. Fall back to a bare mount namespace for the case where user
     * namespaces are administratively disabled but we happen to be
     * privileged; if we are neither, the mount calls fail with a clear error. */
    if (unshare(CLONE_NEWUSER | CLONE_NEWNS) == -1) {
        created_user_ns = 0;
        if (unshare(CLONE_NEWNS) == -1)
            fail("creating a private mount namespace");
    }

    /* Detach our mount tree from the parent's propagation. Without this, on
     * any host where / is shared (the systemd default), the bind/overlay below
     * would propagate back out and change the *caller's* view of /nix/store.
     * In the user-namespace path the kernel already prevents that, but the
     * CLONE_NEWNS-only fallback has no such protection, and mounting over the
     * host's store would be a serious thing to do by accident. */
    if (mount(NULL, "/", NULL, MS_REC | MS_PRIVATE, NULL) == -1)
        fail("making the mount namespace private");

    if (path_exists(store_dir))
        mount_over_store(store_dir, real_store_dir);
    else
        chroot_with_store(store_dir, real_store_dir);

    /* Map our own uid/gid to themselves, so the program sees the identity it
     * would have seen without the namespace -- otherwise it runs as `nobody`
     * and anything that looks itself up (or writes a file expecting to own it)
     * misbehaves.
     *
     * After the mounts, not before: inside a fresh user namespace we hold
     * CAP_SYS_ADMIN over it until the map is written, and writing the map is
     * what settles the identity. `setgroups=deny` is a precondition the kernel
     * imposes on writing gid_map from an unprivileged user namespace.
     *
     * Skipped entirely in the CLONE_NEWNS-only fallback, where there is no new
     * user namespace to map and these writes would fail. */
    if (created_user_ns) {
        write_file("/proc/self/setgroups", "deny");
        snprintf(id_map, sizeof(id_map), "%d %d 1", (int) uid, (int) uid);
        write_file("/proc/self/uid_map", id_map);
        snprintf(id_map, sizeof(id_map), "%d %d 1", (int) gid, (int) gid);
        write_file("/proc/self/gid_map", id_map);
    }

    execvp(args[0], args);
    fprintf(stderr, "%s: executing %s: %s\n", PROG, args[0], strerror(errno));
    return EXIT_EXEC_FAILED;
}
