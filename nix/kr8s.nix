# kr8s, from the umbrella's fork rather than from upstream.
#
# The fork carries changes meant for upstreaming and nothing else -- see
# nixidae's nix/sources.nix and easykubenix issue #29. The largest is
# server-side apply, which upstream has no form for at all, so `ekn` had to
# build the request out of `call_api` by hand. Read `git log main..develop`
# for the rest.
#
# `develop` sits past upstream `main`, which is itself past the v0.20.15
# this used to pin. Same version line, no release between them.
{
  lib,
  buildPythonPackage,
  src,
  # dependencies
  cachetools,
  cryptography,
  exceptiongroup,
  packaging,
  pyyaml,
  python-jsonpath,
  anyio,
  httpx,
  httpx-ws,
  python-box,
  sniffio,
  # build-system
  hatchling,
  hatch-vcs,
}:
buildPythonPackage {
  pname = "kr8s";
  # There is no version to read from `pyproject.toml`: kr8s sets
  # `[tool.hatch.version] source = "vcs"`, so hatch-vcs derives it from a git
  # tag, and the source the umbrella fetches is a tarball with no git history.
  # This attribute is the version -- nixpkgs hands it to hatch-vcs as the
  # pretend version, so it becomes `kr8s.__version__`.
  #
  # **It must be PEP 440.** nixpkgs' own `-unstable-<date>` convention is not,
  # and hatchling refuses it:
  #
  #   packaging.version.InvalidVersion: Error getting the version from source
  #   `vcs`: Invalid version: '0.20.15-unstable-2026-09-17'
  #
  # So: a dated dev release of the next version. A commit count went stale in
  # silence -- this said `dev25` while `develop` was already two commits past
  # it -- and a date says the same thing without claiming to be exact.
  version = "0.20.16.dev20260917";
  pyproject = true;

  inherit src;

  build-system = [
    hatchling
    hatch-vcs
  ];

  dependencies = [
    cachetools
    cryptography
    exceptiongroup
    packaging
    pyyaml
    anyio
    httpx
    httpx-ws
    python-box
    python-jsonpath
    sniffio
  ];

  pythonImportsCheck = [ "kr8s" ];

  meta = with lib; {
    description = "A Python client library for Kubernetes";
    homepage = "https://github.com/kr8s-org/kr8s";
    license = licenses.mit;
    maintainers = with maintainers; [ lillecarl ];
  };
}
