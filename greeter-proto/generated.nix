# greeter-proto's source tree.
#
# The same shape as nanopynix-proto/generated.nix, and for the same reason:
# the derivation produces the *complete* project -- `pyproject.toml` beside
# the `src/greeter` that protoc writes -- so the Python builder gets a plain
# `src` rather than a code-generation step wedged into someone else's build.
# Read that file's header for the full argument, which is not repeated here.
#
# This project exists for one purpose: grpclib-transports' test suite needs a
# real generated gRPC service to talk over each transport, and a hand-written
# stub would not exercise the betterproto2 code path that nanopynix itself
# uses. It ships no production code, and nothing outside
# `grpclib-transports/{tests,benchmarks,docs}` depends on it.
{
  lib,
  runCommand,
  protobuf,
  python,
}:

let
  pythonEnv = python.withPackages (ps: [ ps.betterproto2-compiler ]);
in
runCommand "greeter-proto-source"
  {
    src = lib.fileset.toSource {
      root = ./.;
      fileset = lib.fileset.unions [
        (lib.fileset.fileFilter (file: file.hasExt "proto") ./.)
        ./pyproject.toml
      ];
    };

    nativeBuildInputs = [
      protobuf
      pythonEnv
    ];

    passthru = { inherit pythonEnv; };
  }
  ''
    mkdir -p "$out/src/greeter"
    cp "$src/pyproject.toml" "$out/pyproject.toml"
    # protoc does not emit py.typed, and every module it writes here is fully
    # annotated, so mark the package as typed for the tests that import it.
    touch "$out/src/greeter/py.typed"

    protoc \
      --proto_path="$src" \
      --python_betterproto2_out="$out/src/greeter" \
      --python_betterproto2_opt=client_generation=async \
      --python_betterproto2_opt=server_generation=async \
      --python_betterproto2_opt=google_protobuf_descriptors \
      --python_betterproto2_opt=pydantic_dataclasses \
      common.proto \
      server.proto \
      worker.proto
  ''
