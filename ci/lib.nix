# Minimal function+attrset library of GitHub Actions step constructors,
# rendered to YAML via nanopynix's toYAML primop (see ci/render.py). Workflow-
# and job-level shape (defaults, option types, merging) is handled by
# ci/workflows/schema.nix's lib.evalModules-based schema; this file only
# builds the plain step/list values that get fed into it.
#
# No nixpkgs `lib` dependency on purpose: these step constructors should
# evaluate hermetically from a plain `nix-instantiate --eval`/`eval_.file()`
# call, without needing a working NIX_PATH.
rec {
  optionalAttrs = cond: attrs: if cond then attrs else { };
  optional = cond: x: if cond then [ x ] else [ ];

  # `if` is a Nix keyword, so it can't be a formal-argument name -- callers
  # pass the condition as `cond` and this helper renders it under the
  # literal (quoted) attribute name "if". Works for both step attrs and
  # job attrs.
  withCond = cond: attrs: if cond == null then attrs else attrs // { "if" = cond; };

  steps = {
    checkout =
      { ref ? null }:
      { uses = "actions/checkout@main"; } // optionalAttrs (ref != null) { "with" = { inherit ref; }; };

    installNix = { }: {
      uses = "cachix/install-nix-action@master";
      "with" = {
        extra_nix_config = "experimental-features = nix-command flakes\n";
      };
    };

    cachix =
      { name ? "lillecarl" }:
      {
        uses = "cachix/cachix-action@master";
        "with" = {
          inherit name;
          authToken = "\${{ secrets.CACHIX_AUTH_TOKEN }}";
          useDaemon = false;
        };
      };

    enableSandboxNamespaces =
      { corePattern ? true }:
      {
        name = "Enable Nix sandbox namespaces";
        run = builtins.concatStringsSep "\n" (
          [
            "sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0"
            "sudo sysctl -w kernel.unprivileged_userns_clone=1"
          ]
          ++ optional corePattern "sudo sysctl -w kernel.core_pattern=/tmp/core.%e.%p"
          ++ [
            "unshare --user --map-root-user --mount --pid --fork --mount-proc true"
            ""
          ]
        );
      };

    verifyClosure = { name }: {
      inherit name;
      run = ''nix store verify --recursive --no-trust "$(readlink -f result)"'';
    };

    uploadArtifact =
      {
        name ? null,
        artifactName,
        path,
        cond ? "\${{ !cancelled() }}",
      }:
      withCond cond (
        optionalAttrs (name != null) { inherit name; }
        // {
          uses = "actions/upload-artifact@main";
          "with" = {
            name = artifactName;
            inherit path;
          };
        }
      );

    downloadArtifact = { artifactName }: {
      uses = "actions/download-artifact@main";
      "with" = {
        name = artifactName;
      };
    };
  };
}
