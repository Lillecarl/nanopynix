# The protoc plugin that generates the `nanopynix_proto` and `greeter`
# modules. Runs at build time only (nanopynix-proto/generated.nix and
# greeter-proto/generated.nix), and is never a runtime dependency of anything.
#
# `src` comes from `betterproto2` rather than a second `fetchFromGitHub`: the
# runtime and the compiler are two subdirectories of one repository, and two
# copies of the revision would be two things to keep in step. See
# nix/betterproto2.nix, which states it.
{
  lib,
  buildPythonPackage,
  uv-build,
  betterproto2,
  jinja2,
  ruff,
  typing-extensions,
}:
buildPythonPackage {
  pname = "betterproto2-compiler";
  version = "0.10.1";
  pyproject = true;

  inherit (betterproto2) src;
  sourceRoot = "source/betterproto2_compiler";

  build-system = [ uv-build ];

  dependencies = [
    betterproto2
    jinja2
    ruff
    typing-extensions
  ];

  # The compiler pins an exact `ruff` to format its own output with. nixpkgs
  # carries one `ruff`, this repo lints with that same one, and the generated
  # code is formatted either way -- so the pin is relaxed rather than a second
  # ruff being built for it.
  pythonRelaxDeps = [ "ruff" ];

  pythonImportsCheck = [ "betterproto2_compiler" ];

  meta = {
    description = "protoc plugin that generates betterproto2 Python code";
    homepage = "https://github.com/betterproto/python-betterproto2";
    license = lib.licenses.mit;
    mainProgram = "protoc-gen-python_betterproto2";
    maintainers = [ lib.maintainers.lillecarl ];
  };
}
