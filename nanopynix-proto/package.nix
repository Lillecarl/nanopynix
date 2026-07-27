{
  lib,
  buildPythonPackage,
  protobuf,
  betterproto2,
  betterproto2-compiler,
  googleapis-common-protos,
  grpclib,
  pydantic,
  python,
  renderPyproject,
}:
let
  attrs = renderPyproject {
    projectRoot = toString ./.;
    inherit python;
  };

  # googleapis-common-protos ships the canonical `google/rpc/status.proto`
  # alongside its generated modules, so `google.rpc.Status` -- the message the
  # `grpc-status-details-bin` trailer carries -- is compiled from Google's own
  # definition rather than a hand-copied one. Build-time only: betterproto2
  # emits the result into `nanopynix_proto`, which imports nothing from here.
  googleProtoPath = "${googleapis-common-protos}/${python.sitePackages}";
in
buildPythonPackage (
  attrs
  // {

    src = ./.;

    nativeBuildInputs = [
      protobuf
      betterproto2-compiler
      betterproto2
      googleapis-common-protos
      grpclib
      pydantic
    ];

    pythonImportsCheck = [
      "nanopynix_proto"
      # The generated tree is several packages deep; importing only the root
      # would not notice protoc emitting an unimportable google/ subtree.
      "nanopynix_proto.nix.common"
      "nanopynix_proto.google.rpc"
    ];

    preBuild = ''
      mkdir -p src/nanopynix_proto
      touch src/nanopynix_proto/py.typed
      protoc \
        --proto_path=. \
        --proto_path=${googleProtoPath} \
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
        google/rpc/status.proto
    '';

    meta = attrs.meta // {
      license = lib.licenses.lgpl21Plus;
      platforms = lib.platforms.unix;
    };
  }
)
