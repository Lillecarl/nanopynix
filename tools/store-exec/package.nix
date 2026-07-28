{
  lib,
  stdenv,
  glibc,
}:
stdenv.mkDerivation {
  pname = "nanopynix-store-exec";
  version = "0-unstable";
  src = ./.;

  # Statically linked on purpose. This program's whole job is to rearrange the
  # mount table and then exec, and part of that rearrangement can be a chroot
  # into a tree that does not contain the dynamic loader. A static binary has
  # no such ordering hazard, and it also makes the security story trivial to
  # state: the only code that runs with the namespace's capabilities is the
  # code in store-exec.c. Nothing here uses NSS (no getpwnam/getaddrinfo), so
  # the usual glibc static-linking caveat does not apply.
  buildInputs = [ glibc.static ];

  dontConfigure = true;

  buildPhase = ''
    runHook preBuild
    $CC -static -O2 -Wall -Wextra -Werror -o nanopynix-store-exec store-exec.c
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    install -Dm755 nanopynix-store-exec $out/bin/nanopynix-store-exec
    runHook postInstall
  '';

  meta = {
    description = "Run a program from a relocated Nix store with that store at its logical path";
    mainProgram = "nanopynix-store-exec";
    platforms = lib.platforms.linux;
  };
}
