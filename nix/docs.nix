{
  lib,
  stdenvNoCC,
  pythonSet,
}:
let
  # Built, not editable: this is a buildable artifact, so its inputs have to
  # come from the store. `pynix` here is the *library* out of the package set,
  # not the `mkApplication` wrapper of the same name in default.nix -- autodoc
  # imports modules, and an application exposes only `bin/`.
  pythonEnv = pythonSet.mkVirtualEnv "nanopynix-docs-env" {
    pynix = [ "docs" ];
  };
in
stdenvNoCC.mkDerivation {
  pname = "nanopynix-docs";
  version = "0";

  # Filtered, and not a bare `../.`: see `nix/source.nix`. Sphinx reads `docs/`
  # and imports `pynix`, so a cache directory only adds weight to the copy.
  src = import ./source.nix { inherit lib; };

  nativeBuildInputs = [ pythonEnv ];

  buildPhase = ''
    runHook preBuild
    NANOPYNIX_DOCS_OFFLINE=1 sphinx-build -b html docs "$out" -W
    runHook postBuild
  '';

  dontInstall = true;
}
