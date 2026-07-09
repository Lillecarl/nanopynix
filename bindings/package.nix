{
  lib,
  buildPythonPackage,
  pkg-config,
  python,
  nanobind,
  nix,
  cmake,
  ninja,
  scikit-build-core,
}:

let
  # nanobind 2.12.0 stubgen never increments pattern.matches in
  # apply_pattern(), so every .pat rule emits a false "did not match
  # any elements" warning even when it was applied successfully.
  # Patch the counter so the warning logic works correctly.
  nanobindPatched = nanobind.overrideAttrs (old: {
    postFixup = (old.postFixup or "") + ''
      find $out -name stubgen.py -path '*/nanobind/*' -exec \
        sed -i '/# Success, pattern was applied/a\        pattern.matches += 1' {} \;
    '';
  });
in

buildPythonPackage {
  pname = "nanopynix-bindings";
  version = "0.1.0";
  pyproject = true;

  src = lib.cleanSourceWith {
    filter =
      path: type:
      let
        baseName = lib.baseNameOf path;
      in
      lib.cleanSourceFilter path type && baseName != "tests";
    src = ./.;
  };

  build-system = [
    cmake
    ninja
    scikit-build-core
  ];

  nativeBuildInputs = [
    pkg-config
  ];
  buildInputs =
    let
      recursivePropagation =
        derivations:
        lib.concatMap (
          x:
          if x.buildInputs or null != null then
            [ x ] ++ x.buildInputs ++ recursivePropagation x.buildInputs
          else
            [ ]
        ) derivations;
    in
    [
      nanobindPatched
    ]
    ++ lib.pipe nix.libs [
      lib.attrsToList
      (lib.map ({ value, ... }: value))
      recursivePropagation
      lib.unique
    ];

  dontUseCmakeConfigure = true;

  cmakeFlags = [
    "-Dnanobind_ROOT=${nanobindPatched}/${python.sitePackages}/nanobind/cmake"
    "-DPython_EXECUTABLE=${python}/bin/python"
  ];

  postInstall = ''
    _site="$out/${python.sitePackages}"
    for mod in nanopynix_util nanopynix_store nanopynix_expr nanopynix_fetchers nanopynix_flake nanopynix_main; do
      _pat=""
      if [ -f "src/$mod.pat" ]; then
        _pat="-p src/$mod.pat"
      fi
      PYTHONPATH="$_site:$PYTHONPATH" \
        ${python}/bin/python -m nanobind.stubgen -m "$mod" $_pat -O "$_site"
    done
  '';

  pythonImportsCheck = [
    "nanopynix_util"
    "nanopynix_store"
    "nanopynix_expr"
    "nanopynix_fetchers"
    "nanopynix_flake"
    "nanopynix_main"
  ];

  meta = with lib; {
    description = "nanobind-based Python bindings for Nix (compiled extensions)";
    license = licenses.lgpl21Plus;
    platforms = platforms.unix;
  };
}
