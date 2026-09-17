# kr8s, from the umbrella's fork rather than from upstream.
#
# The fork carries changes meant for upstreaming and nothing else -- see
# nixidae's nix/sources.nix and easykubenix issue #29. The largest is
# server-side apply, which upstream has no form for at all, so `ekn` had to
# build the request out of `call_api` by hand. Read `git log main..develop`
# for the rest.
#
# `develop` sits 5 commits past upstream `main`, which is 25 commits past
# the v0.20.15 this used to pin. Same version line.
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
  version = "0.20.16.dev30";
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
