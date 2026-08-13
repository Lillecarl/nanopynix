#pragma once

#define NANOPYNIX_NIX_VERSION "@NIX_STORE_VERSION@"
#define NANOPYNIX_NIX_VERSION_MAJOR @NANOPYNIX_NIX_VERSION_MAJOR@
#define NANOPYNIX_NIX_VERSION_MINOR @NANOPYNIX_NIX_VERSION_MINOR@
#define NANOPYNIX_NIX_VERSION_NUMBER @NANOPYNIX_NIX_VERSION_NUMBER@

// One name for each version this repository compares against.
//
// `NANOPYNIX_NIX_2_32` and `NANOPYNIX_NIX_2_34` went with issue #129:
// `supportedNixFloor` in `default.nix` is 2.34, so every `< 2.32` branch was
// dead and every `>= 2.34` branch was always taken. Add a name here when a new
// version needs one, and delete a name when the floor rises past it -- a
// constant that no `#if` reads is a version this build still appears to
// support.
#define NANOPYNIX_NIX_2_35 2035
