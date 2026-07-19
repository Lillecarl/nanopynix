{
  lib,
  buildPythonPackage,
  protobuf,
  betterproto2,
  betterproto2-compiler,
  grpclib,
  pydantic,
  python,
  renderPyproject,
}:
let
  attrs = renderPyproject {
    projectRoot = ./.;
    inherit python;
    pythonPackages = python.pkgs // {
      inherit betterproto2;
    };
  };
in
buildPythonPackage (
  attrs
  // {

    src = lib.cleanSource ./.;

    nativeBuildInputs = [
      protobuf
      betterproto2-compiler
      betterproto2
      grpclib
      pydantic
    ];

    pythonImportsCheck = [
      "nanopynix_proto"
    ];

    preBuild = ''
      mkdir -p src/nanopynix_proto
      touch src/nanopynix_proto/py.typed
      protoc \
        --proto_path=. \
        --python_betterproto2_out=src/nanopynix_proto \
        --python_betterproto2_opt=client_generation=async \
        --python_betterproto2_opt=server_generation=async \
        --python_betterproto2_opt=google_protobuf_descriptors \
        --python_betterproto2_opt=pydantic_dataclasses \
        common.proto \
        store.proto \
        eval.proto \
        worker.proto \
        manager.proto \
        daemon.proto
    '';

    meta = attrs.meta // {
      license = lib.licenses.lgpl21Plus;
      platforms = lib.platforms.unix;
    };
  }
)
