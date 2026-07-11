{
  lib,
  buildPythonPackage,
  hatchling,
  protobuf,
  betterproto2,
  betterproto2-compiler,
  grpclib,
  pydantic,
}:

buildPythonPackage {
  pname = "nanopynix-proto";
  version = "0.1.0";
  pyproject = true;

  src = lib.cleanSource ./.;

  build-system = [ hatchling ];

  nativeBuildInputs = [
    protobuf
    betterproto2-compiler
    betterproto2
    grpclib
    pydantic
  ];

  dependencies = [
    betterproto2
    protobuf
    pydantic
    grpclib
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
      common.proto \
      store.proto \
      eval.proto \
      worker.proto
  '';

  meta = with lib; {
    description = "gRPC protobuf definitions for nanopynix (betterproto2 compiled)";
    license = licenses.lgpl21Plus;
    platforms = platforms.unix;
  };
}