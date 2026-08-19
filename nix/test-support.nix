# `test-support`, built the nixpkgs way, for the one consumer that cannot take
# the pyproject.nix builders package.
#
# Every project here reaches `test_support` through `pythonSet`, which
# nix/python-set.nix builds with pyproject.nix's builders.
# `completion-spike` cannot: it is a nixpkgs `buildPythonApplication`, because
# `pytestCheckHook` is what runs its suite inside its own build, and a builders
# package is not an input that a nixpkgs build can take. Its suite drives three
# shells through `test_support.shell_pty`.
#
# So the same source is built twice, by the two infrastructures. That is
# cheaper than the two alternatives: a second copy of the pty driver, or a
# check phase bolted onto a builders package that has none.
{
  lib,
  buildPythonPackage,
  hatchling,
  anyio,
  pygit2,
  pexpect,
}:

buildPythonPackage {
  pname = "test-support";
  version = "0.1.0";
  pyproject = true;

  src = lib.fileset.toSource {
    root = ../test-support;
    fileset = lib.fileset.unions [
      ../test-support/pyproject.toml
      ../test-support/LICENSE
      ../test-support/src
    ];
  };

  build-system = [ hatchling ];
  dependencies = [
    anyio
    pygit2
    pexpect
  ];

  # `checks.test-support` runs this project's own suite, against the builders
  # package that every other project takes. A second run of the same tests
  # here would report the packaging and not the code.
  doCheck = false;

  pythonImportsCheck = [ "test_support.shell_pty" ];

  meta = {
    description = "Test helpers with no Nix knowledge, shared by every project in this repository";
    license = lib.licenses.asl20;
    maintainers = [ lib.maintainers.lillecarl ];
  };
}
