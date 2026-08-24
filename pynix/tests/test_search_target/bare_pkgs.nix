# A target that is a package set and no module system. A person may point at
# one, and package search still answers. `path` and `stdenv` are the two
# markers that say so.
{ }:
(import ../../../. { }).pkgs
