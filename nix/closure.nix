# The named packages of `pkgs`, rebuilt with the stdenv of the wheel.
#
# A wheel carries every library that the extension links, so every one of those
# libraries must take the same glibc floor and the same C++ runtime. One
# `.override` per package is not enough on its own: an overridden package would
# still take its own dependencies from the stdenv of nixpkgs, and one library
# built the ordinary way puts the whole wheel back on `GLIBC_2.38`.
#
# `pkgs.extend` is the wrong instrument here, and the reason is worth recording.
# An overlay recomputes the whole fixed point, so a new `zlib` also gives a new
# `perl` and a new `bison`. Those two are build tools. They never enter the
# wheel, they leave the binary cache when they change, and one of them then runs
# its own test suite on a shared builder.
#
# So this file wires the graph by hand, and touches nothing else. `lib.fix`
# gives each package the rebuilt version of every dependency that is *also* in
# the list, and `__functionArgs` says which dependencies a package will accept.
# `nativeBuildInputs` keep resolving through the original `pkgs`, because
# `.override` replaces only the arguments that it names.
{
  lib,
  pkgs,
  # The stdenv of `nix/cxx-stdenv.nix`.
  stdenv,
  # The name in `pkgs` of each package that must move onto that stdenv.
  packages,
  # Further arguments for one package, by name. The payload trim goes here,
  # for example `{ boost = { enableIcu = false; }; }`.
  extraArgs ? { },
  # An `overrideAttrs` function for one package, by name. Use this where the
  # correction is not an argument of the package.
  extraAttrs ? { },
}:
lib.fix (
  self:
  lib.genAttrs packages (
    name:
    let
      package = pkgs.${name};
      declared = package.override.__functionArgs or { };
      # A package takes the rebuilt version of a dependency only when it
      # declares that dependency as an argument. A package never takes itself.
      dependencies = lib.filterAttrs (dependency: _: declared ? ${dependency}) (
        removeAttrs self [ name ]
      );
    in
    # A package with no `.override` cannot move onto another stdenv, so it
    # would keep the floor of nixpkgs and put the whole wheel back on
    # `GLIBC_2.38`. Stop here rather than let that pass. `libidn2` is such a
    # package: nixpkgs uses it to bootstrap `fetchurl`. Drop the feature that
    # needs it instead, the way `curl` drops `idnSupport`.
    assert lib.assertMsg (package ? override) ''
      `${name}` has no `override`, so it cannot move onto the wheel stdenv.

      A build of it with the stdenv of nixpkgs would keep its own `GLIBC_2.38`
      floor, and the wheel takes the highest floor of everything that it
      carries. Remove the feature that needs `${name}`, or give the package an
      `overrideAttrs` that reaches the same result.
    '';
    # **An argument that a package does not declare is dropped in silence.**
    # `.override` keeps only the arguments of the function it wraps, so a
    # rename in nixpkgs turns a setting here into nothing at all and says so
    # nowhere. Measured: nixpkgs made `pkgs.curl` a wrapper whose one argument
    # is `curlMinimal`, and `gssSupport`, `idnSupport` and `pslSupport` stopped
    # applying. The wheel then carried `libidn2` again, and `curl` took the
    # openssl of nixpkgs rather than the rebuilt one, which put the whole wheel
    # back on `GLIBC_2.38`. Issue #220.
    assert
      let
        undeclared = lib.subtractLists (lib.attrNames declared) (lib.attrNames (extraArgs.${name} or { }));
      in
      lib.assertMsg (undeclared == [ ]) ''
        `${name}` does not declare ${lib.concatStringsSep ", " undeclared}, so `extraArgs.${name}` would do nothing.

        `.override` keeps only the arguments of the function it wraps. nixpkgs
        has renamed the argument, or moved the real package behind a wrapper.
        Name the package that declares the argument, and put it in `packages`
        as well so that the wrapper takes the rebuilt one.
      '';
    lib.pipe package [
      (built: built.override (dependencies // { inherit stdenv; } // (extraArgs.${name} or { })))
      (built: if extraAttrs ? ${name} then built.overrideAttrs extraAttrs.${name} else built)
    ]
  )
)
