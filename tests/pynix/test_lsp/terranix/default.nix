# A terranix -> OpenTofu example: terranix renders this directory's Nix
# modules (./modules) into a `config.tf.json`, the same way it would for a
# real deployment -- this is the fixture pynix-lsp's (future) opt-in
# Terraform-provider-schema support is meant to point at, since
# `tofu providers schema -json` run against a real `.terraform.lock.hcl` is
# where provider resource/argument schemas come from.
#
# random/local/null are all fully offline providers -- no cloud credentials,
# no network access needed at plan/apply time -- so `tofu` here runs
# standalone, in CI/sandboxes included.
{ }:
let
  default = import ../../../../. { };
  inherit (default) pkgs;
  inherit (pkgs) lib;

  providerNames = [
    "random"
    "local"
    "null"
  ];

  provider = name: pkgs.terraform-providers."hashicorp_${name}";
  plugins = map provider providerNames;

  # OpenTofu's `withPlugins` wrapper (see nixpkgs' opentofu package.nix)
  # rewrites the plugin tree it builds to `registry.opentofu.org`, which is
  # exactly the host OpenTofu resolves an unqualified "hashicorp/<name>"
  # `required_providers` source to by default -- so this needs no explicit
  # registry host, and no network access, to resolve.
  requiredProvidersModule = {
    config.terraform.required_providers = lib.listToAttrs (
      map (name: {
        inherit name;
        value = {
          source = "hashicorp/${name}";
          version = (provider name).version;
        };
      }) providerNames
    );
  };

  # Declared (empty) rather than left implicit: an *unconfigured* local
  # backend ignores `-backend-config` entirely and always writes
  # "terraform.tfstate" relative to the `-chdir` target (the read-only
  # store path) -- confirmed by `tofu apply` failing with "read-only file
  # system" without this block present. Declaring the block, even empty,
  # makes `tofu`'s `init -backend-config="path=$TF_DATA_DIR/..."` (below)
  # actually take effect and persist into $TF_DATA_DIR for later
  # plan/apply.
  backendModule = {
    config.terraform.backend.local = { };
  };

  terranix = import "${pkgs.terranix.src}/core" {
    inherit pkgs;
    modules = [
      ./modules/random.nix
      ./modules/local.nix
      ./modules/null.nix
      requiredProvidersModule
      backendModule
    ];
  };

  configJSON = pkgs.writeText "config.tf.json" (builtins.toJSON terranix.config);

  tofuUnwrapped = pkgs.opentofu.withPlugins (_: plugins);

  # config.tf.json plus a matching .terraform.lock.hcl, baked in by a real
  # `tofu init -backend=false` -- fully offline, since every provider above
  # is already local via `withPlugins`. Nothing else lives here: the
  # writable `.terraform` provider/state directory belongs outside the
  # store (see `tofu` below, via $TF_DATA_DIR), so this path can stay
  # read-only at runtime.
  module =
    pkgs.runCommand "terranix-demo-module"
      {
        nativeBuildInputs = [ tofuUnwrapped ];
      }
      ''
        mkdir -p "$out"
        cp ${configJSON} "$out/config.tf.json"
        cd "$out"
        TF_DATA_DIR=$TMPDIR/.terraform tofu init -backend=false -input=false
      '';

  # Wraps the plugin-bundled `tofu` so it always operates on `module` (in
  # the store, read-only) via `-chdir`, while keeping its own writable
  # state -- provider install dir, plan cache, tfstate -- outside it,
  # relative to wherever this script is actually invoked from (not the
  # `-chdir` target: `-chdir` changes directory before running, so relative
  # paths computed after that point would resolve inside the read-only
  # store path otherwise).
  #
  # $TF_DATA_DIR (defaults to `./.terraform`) is Terraform's own working
  # directory: provider installs, plan cache, and -- for any *non*-local
  # backend -- a small "which backend, which config" pointer file at
  # exactly `$TF_DATA_DIR/terraform.tfstate`. That reserved path must NOT
  # also be used for the local backend's actual state file: the local
  # backend was first pointed at $TF_DATA_DIR/terraform.tfstate here too,
  # and after one `apply` every later command failed with "does not support
  # CLI state version 4" -- `apply` had overwritten the pointer file (a
  # small "version": 3 JSON) with real resource state (a "version": 4
  # JSON), and `init`'s own reader tried to parse the latter as the former.
  # $TF_STATE_PATH (defaults to `./terraform.tfstate`, a sibling of
  # $TF_DATA_DIR rather than inside it) is that separate, real, state file.
  # `init` is the only command that needs either told explicitly -- both
  # choices persist into $TF_DATA_DIR for later plan/apply.
  tofu = pkgs.writeShellApplication {
    name = "tofu";
    runtimeInputs = [ tofuUnwrapped ];
    text = ''
      export TF_DATA_DIR="''${TF_DATA_DIR:-$PWD/.terraform}"
      TF_STATE_PATH="''${TF_STATE_PATH:-$PWD/terraform.tfstate}"
      mkdir -p "$TF_DATA_DIR"
      if [ "''${1:-}" = "init" ]; then
        shift
        exec tofu -chdir=${module} init -backend-config="path=$TF_STATE_PATH" "$@"
      else
        exec tofu -chdir=${module} "$@"
      fi
    '';
  };
in
{
  inherit
    pkgs
    module
    terranix
    tofu
    ;
}
