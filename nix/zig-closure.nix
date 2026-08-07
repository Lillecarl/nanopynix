# The named packages of `pkgs`, rebuilt with the zig stdenv.
#
# A wheel carries every library that the extension links, so every one of those
# libraries must take the same glibc floor. One `.override` per package is not
# enough on its own: an overridden package would still take its own
# dependencies from the stdenv of nixpkgs, and one gcc-built library in the
# graph puts the whole wheel back on `GLIBC_2.38`.
#
# `pkgs.extend` is the wrong instrument here, and the reason is worth recording.
# An overlay recomputes the whole fixed point, so a new `zlib` also gives a new
# `perl` and a new `bison`. Those two are build tools. They never enter the
# wheel, they leave the binary cache when they change, and one of them then runs
# its own test suite on a shared builder.
#
# So this file wires the graph by hand, and touches nothing else. `lib.fix`
# gives each package the zig build of every dependency that is *also* in the
# list, and `__functionArgs` says which dependencies a package will accept.
# `nativeBuildInputs` keep resolving through the original `pkgs`, because
# `.override` replaces only the arguments that it names.
{
  lib,
  pkgs,
  zigStdenv,
  # The name in `pkgs` of each package that must move onto the zig stdenv.
  packages,
  # Further arguments for one package, by name. The payload trim goes here,
  # for example `{ boost = { enableIcu = false; }; }`.
  extraArgs ? { },
  # An `overrideAttrs` function for one package, by name. Use this where the
  # correction is not an argument of the package, such as a warning that clang
  # gives and gcc does not.
  extraAttrs ? { },
}:
lib.fix (
  self:
  lib.genAttrs packages (
    name:
    let
      package = pkgs.${name};
      declared = package.override.__functionArgs or { };
      # A package takes the zig build of a dependency only when it declares
      # that dependency as an argument. A package never takes itself.
      dependencies = lib.filterAttrs (
        dependency: _: declared ? ${dependency}
      ) (removeAttrs self [ name ]);
    in
    # A package with no `.override` cannot move onto another stdenv, so it
    # would stay a gcc build and put the whole wheel back on `GLIBC_2.38`.
    # Stop here rather than let that pass. `libidn2` is such a package:
    # nixpkgs uses it to bootstrap `fetchurl`. Drop the feature that needs it
    # instead, the way `curl` drops `idnSupport`.
    assert lib.assertMsg (package ? override) ''
      `${name}` has no `override`, so it cannot move onto the zig stdenv.

      A gcc build of it would keep its own `GLIBC_2.38` floor, and the wheel
      takes the highest floor of everything that it carries. Remove the
      feature that needs `${name}`, or give the package an `overrideAttrs`
      that reaches the same result.
    '';
    lib.pipe package [
      (built: built.override (dependencies // { stdenv = zigStdenv; } // (extraArgs.${name} or { })))
      (built: if extraAttrs ? ${name} then built.overrideAttrs extraAttrs.${name} else built)
    ]
  )
)
