#pragma once

#define NANOPYNIX_NIX_VERSION "@NIX_STORE_VERSION@"
#define NANOPYNIX_NIX_VERSION_MAJOR @NANOPYNIX_NIX_VERSION_MAJOR@
#define NANOPYNIX_NIX_VERSION_MINOR @NANOPYNIX_NIX_VERSION_MINOR@
#define NANOPYNIX_NIX_VERSION_NUMBER @NANOPYNIX_NIX_VERSION_NUMBER@

#define NANOPYNIX_NIX_2_32 2032
#define NANOPYNIX_NIX_2_35 2035
// `builder-rpc-v0`, which NixOS/nix#15793 merged on 2026-07-21, after 2.35.
// It reaches no release yet, so `nix/nix-master.nix` is the only source that
// selects this band. Read `ddrn/README.md` for what it is.
#define NANOPYNIX_NIX_2_36 2036
