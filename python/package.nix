{
  lib,
  callPackage,
  buildPythonPackage,
  # cool deps
  nanopynix-bindings ? callPackage ../bindings/package.nix { },
  nanopynix-proto ? callPackage ../proto/package.nix { },
  grpclib-transports,
  python,
  renderPyproject,
  enableTsan ? false,
  tsanRuntime ? null,
}:
let
  attrs = renderPyproject {
    projectRoot = ./.;
    inherit python;
    pythonPackages = python.pkgs // {
      "nanopynix-bindings" = nanopynix-bindings;
      "nanopynix-proto" = nanopynix-proto;
      "grpclib-transports" = grpclib-transports;
    };
  };
in
buildPythonPackage (
  attrs
  // {

    src = lib.cleanSource ./.;

    # pythonImportsCheck dlopen()s nanopynix-bindings transitively into a
    # fresh python process; when bindings were built with TSAN, its runtime
    # must be preloaded here too or the same static-TLS failure occurs (see
    # bindings/package.nix's own env.LD_PRELOAD for why).
    env = lib.optionalAttrs (enableTsan && tsanRuntime != null) {
      LD_PRELOAD = tsanRuntime;
    };

    pythonImportsCheck = [
      "nanopynix"
    ];

    meta = attrs.meta // {
      license = lib.licenses.lgpl21Plus;
      platforms = lib.platforms.unix;
    };
  }
)
