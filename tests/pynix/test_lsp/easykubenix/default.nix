# An easykubenix (https://github.com/Lillecarl/easykubenix) example: a
# NixOS-module-style system for generating Kubernetes manifests.
#
# Goes through easykubenix's own real `default.nix` entry point (not a
# hand-rolled `lib.evalModules` call) -- unlike terranix, easykubenix's
# `default.nix` already exposes the *raw* `lib.evalModules` result via
# `passthru.eval`: real `config`/`options`/`_module`, no un-hiding
# workaround needed (see `../terranix/default.nix`'s `moduleSystem` comment
# for what terranix has to work around instead). That makes `moduleSystem`
# below directly usable as the `# pynix-lsp: easykubenixEntry = ...` target's
# `moduleSystem` field.
#
# `openApiSchemaPath` is a pinned upstream v1.33 Kubernetes OpenAPI v2
# (`swagger.json`-shaped) document -- same major.minor as
# `easykubenix/apiResources/v1.33.json`, the Kind->apiVersion mapping file
# `kubernetes.nix` already defaults to, so a Kind resolved via
# `config.kubernetes.apiMappings` is guaranteed to also exist in this
# schema's `definitions`. Real easykubenix projects have other options here
# (a live-cluster `kubectl get --raw /openapi/v2` dump cached to disk, which
# also picks up installed CRDs a static release schema can never see) --
# `EasykubenixDialect` (see `pynix/src/pynix/_lsp/_easykubenix.py`) only
# ever consumes the resulting path, indifferent to how it was produced.
{ }:
let
  default = import ../../../../. { };
  inherit (default) pkgs;

  flake = import ../../../../nix/compat.nix;
  easykubenixSrc = flake.inputs.easykubenix;

  testModules = [
    ./modules/demo.nix
    ./modules/config.nix
  ];

  eku = import easykubenixSrc {
    inherit pkgs;
    modules = testModules;
  };

  k8sOpenApiSchema = pkgs.fetchurl {
    url = "https://raw.githubusercontent.com/kubernetes/kubernetes/release-1.33/api/openapi-spec/swagger.json";
    sha256 = "1xkykxxhk72n3x01df0jm9pmw24q2rzizwqnwncibbslhpwspx9a";
  };
in
{
  inherit pkgs;
  moduleSystem = eku.passthru.eval;
  openApiSchemaPath = "${k8sOpenApiSchema}";

  # The derivation behind `openApiSchemaPath`, exposed so the test suite can
  # realise it. Interpolating it above yields a store path but never builds
  # one, and nothing downstream builds either -- the LSP only *evaluates* this
  # file, then reads the path off disk. Without a `nix-build -A openApiSchema`
  # first, every easykubenix scenario fails with FileNotFoundError on a path
  # that is perfectly valid and simply absent. See
  # `tests/pynix/conftest.py`'s `easykubenix_openapi_schema` fixture.
  openApiSchema = k8sOpenApiSchema;
}
