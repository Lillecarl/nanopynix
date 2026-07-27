{
  lib,
  fetchFromGitHub,
  buildPythonPackage,
  setuptools,
  wheel,
  beartype,
  pytest,
}:
buildPythonPackage (finalAttrs: {
  pname = "pytest-beartype";
  version = "0.2.0";
  pyproject = true;
  src = fetchFromGitHub {
    owner = "beartype";
    repo = "pytest-beartype";
    tag = "v${finalAttrs.version}";
    hash = "sha256-fcJEDetynP8ekrI6Z71Eaf3jQBeccDRj7A6GeJalYKw=";
  };
  build-system = [
    setuptools
    wheel
  ];
  dependencies = [
    beartype
    pytest
  ];
  pythonImportsCheck = [ "pytest_beartype" ];
  meta = {
    description = "Pytest plugin to run your tests with beartype checking enabled";
    homepage = "https://github.com/beartype/pytest-beartype";
    license = lib.licenses.mit;
    maintainers = [ lib.maintainers.lillecarl ];
  };
})
