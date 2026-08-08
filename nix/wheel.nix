# The PyPI wheel: `auditwheel repair` over the zig build of the extension.
#
# `nanopynixZig.nanopynix-bindings` builds an extension whose libraries live in
# the Nix store, and it links each one through an RPATH. A wheel cannot do
# that. `auditwheel repair` copies each library into `nanopynix_bindings.libs/`,
# gives each file name a hash suffix, rewrites every RPATH to `$ORIGIN`, and
# writes the `manylinux` tag onto the wheel.
#
# It also *checks*. It reads the versioned symbols of every object and refuses
# a tag that the objects do not support, so the tag on the product is a
# measurement and not a claim. Issue #111 holds the run.
{
  lib,
  stdenv,
  runCommand,
  auditwheel,
  patchelf,
  # The zig build. A gcc build gets a `manylinux_2_38` tag, which is what this
  # whole closure exists to avoid.
  bindings,
  # The same number as `nix/zig-stdenv.nix` targets. `auditwheel` refuses a tag
  # that the objects do not support, so a mismatch here fails the build rather
  # than shipping a wheel that does not load.
  glibcVersion ? "2.34",
}:
let
  # A wheel names the architecture the way `uname -m` does, which is what
  # `parsed.cpu.name` holds: `x86_64` and `aarch64`. Do not write this by hand.
  # It was `_x86_64` at first, and an `aarch64` build then produced a
  # derivation called `...manylinux_2_34_x86_64`, which is a wheel that pip
  # installs on the wrong machine.
  architecture = stdenv.hostPlatform.parsed.cpu.name;
  platform = "manylinux_${lib.replaceStrings [ "." ] [ "_" ] glibcVersion}_${architecture}";
in
runCommand "nanopynix-bindings-wheel-${platform}"
  {
    nativeBuildInputs = [
      auditwheel
      patchelf
    ];

    # `bindings` itself, and not `bindings.dist` alone. The `.so` inside the
    # wheel names each library in an RPATH, and `auditwheel` has to open those
    # files. A wheel is a zip, so the store paths inside it are compressed and
    # the reference scanner of Nix does not see them: `dist` therefore carries
    # no reference to the libraries, and the sandbox would hold none of them.
    # The installed output holds the same `.so` uncompressed, so naming it
    # brings the whole closure in.
    buildInputs = [ bindings ];

    inherit platform;

    meta = {
      description = "nanopynix-bindings as a ${platform} wheel";
      inherit (bindings.meta) license;
    };
  }
  ''
    mkdir -p $out

    cp ${bindings.dist}/*.whl .
    chmod +w ./*.whl

    echo "--- before ---"
    auditwheel show ./*.whl

    auditwheel repair --plat "$platform" -w "$out" ./*.whl

    echo "--- after ---"
    auditwheel show "$out"/*.whl
  ''
