# A small attribute set shaped like the top level of nixpkgs, for the package
# walk to read. It is real: `lib` and `pkgs` come from this repository, and
# each package below is a real derivation.
#
# Four of them are traps that a walk of real nixpkgs meets, and each one must
# not end the extraction:
#
#   `broken`   states `meta.broken`;
#   `unfree`   states an unfree licence;
#   `throwing` throws when it is evaluated at all;
#   `notADerivation` is an ordinary attribute, and no package.
{ }:
let
  default = import ../../../. { };
  inherit (default) pkgs lib;
in
{
  inherit lib;

  ripgrep = pkgs.runCommand "ripgrep-14.1.1" {
    meta = {
      description = "Recursively search directories for a regex pattern";
      mainProgram = "rg";
    };
  } "touch $out" // { pname = "ripgrep"; version = "14.1.1"; };

  # A package that names no `mainProgram`. 8 502 of 24 571 real ones do not.
  hello-no-main = pkgs.runCommand "hello-2.12" {
    meta.description = "A program that produces a familiar greeting";
  } "touch $out" // { pname = "hello"; version = "2.12"; };

  broken = pkgs.runCommand "broken-1.0" {
    meta = {
      description = "A package that nixpkgs marks broken";
      broken = true;
    };
  } "touch $out" // { pname = "broken"; version = "1.0"; };

  unfree = pkgs.runCommand "unfree-1.0" {
    meta = {
      description = "A package whose licence is not free";
      license = { free = false; };
    };
  } "touch $out" // { pname = "unfree"; version = "1.0"; };

  # Evaluating this attribute at all raises. `builtins.tryEval` in the walk is
  # what stops it ending the extraction of every other package.
  throwing = throw "this package cannot be evaluated";

  # Not a derivation, so the walk skips it rather than recording it.
  notADerivation = { some = "attrset"; };

  inherit (default) pkgs;
}
