# betterproto2, the protobuf runtime the generated `nanopynix-proto` and
# `greeter-proto` modules import.
#
# Not in nixpkgs, which is why it is here. It arrived with the
# `grpclib-transports` flake input and stayed behind when that input was
# dropped and the project was vendored -- see the grpclib-transports section
# of AGENTS.md.
#
# `nix/betterproto2-compiler.nix` is the protoc plugin from the same source
# tree and reads `src` back off this derivation, so the revision and the hash
# are stated once, here.
{
  lib,
  buildPythonPackage,
  fetchFromGitHub,
  uv-build,
  python-dateutil,
  typing-extensions,
  pydantic,
  grpclib,
}:
buildPythonPackage {
  pname = "betterproto2";
  version = "0.10.0";
  pyproject = true;

  src = fetchFromGitHub {
    owner = "betterproto";
    repo = "python-betterproto2";
    rev = "25e2893cf83e160b19389af2f469341ff864ea18";
    hash = "sha256-Xv8xdSh0C95xMqOF8mR7wuKe/mcbo5IDExEZsCcn9fo=";
  };

  # One repository holds both the runtime and the compiler, each in its own
  # subdirectory with its own pyproject.toml.
  sourceRoot = "source/betterproto2";

  build-system = [ uv-build ];

  dependencies = [
    python-dateutil
    typing-extensions
    pydantic
    grpclib
  ];

  pythonImportsCheck = [ "betterproto2" ];

  meta = {
    description = "Protobuf and gRPC runtime for Python, with dataclasses";
    homepage = "https://github.com/betterproto/python-betterproto2";
    license = lib.licenses.mit;
    maintainers = [ lib.maintainers.lillecarl ];
  };
}
