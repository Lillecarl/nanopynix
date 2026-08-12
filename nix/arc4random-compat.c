/* `arc4random` and `arc4random_buf`, for a target below glibc 2.36.
 *
 * glibc added both functions in 2.36, and `libstdc++.a` calls `arc4random` from
 * `std::random_device`. The wheel targets glibc 2.34, so a link against the
 * glibc of nixpkgs puts `arc4random@GLIBC_2.36` in the runtime and the library
 * then fails to load on the oldest host that the wheel claims.
 *
 * `nix/cxx-runtime.nix` links this file **before** `libstdc++.a`, so the linker
 * binds that call to the definition here and writes no versioned reference to
 * glibc at all.
 *
 * `getrandom` arrived in glibc 2.25, which is below the floor, and it reads the
 * same kernel source that `arc4random` reads. The two functions differ in that
 * `arc4random` keeps a userspace stream cipher and this one does not, so this
 * one makes a system call for each request. `std::random_device` seeds a
 * generator and is not a hot path, so the cost does not reach a caller.
 */

#include <stddef.h>
#include <stdint.h>
#include <sys/random.h>

void arc4random_buf(void *buffer, size_t length) {
    unsigned char *out = buffer;

    while (length > 0) {
        ssize_t got = getrandom(out, length, 0);

        /* `getrandom` returns EINTR when a signal arrives before the pool has
         * enough entropy, and a short read when the request is large. Neither
         * is an error, and neither is a reason to give a caller a buffer that
         * this function did not fill. There is no failure to report: the
         * signature returns void, and every caller treats the buffer as
         * random. */
        if (got < 0) {
            continue;
        }

        out += (size_t) got;
        length -= (size_t) got;
    }
}

uint32_t arc4random(void) {
    uint32_t value;

    arc4random_buf(&value, sizeof value);
    return value;
}
