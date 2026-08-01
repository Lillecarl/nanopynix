# Minimal function+attrset library of GitHub Actions step constructors.
# Workflow- and job-level shape (defaults, option types, merging) is handled
# by schema.nix's lib.evalModules-based schema; this file only builds the
# plain step/list values that get fed into it.
{ lib }:
rec {
  # `if` is a Nix keyword, so it can't be a formal-argument name -- callers
  # pass the condition as `cond` and this helper renders it under the
  # literal (quoted) attribute name "if". Works for both step attrs and
  # job attrs.
  withCond = cond: attrs: if cond == null then attrs else attrs // { "if" = cond; };

  steps = {
    # `fetchDepth = 0` fetches the whole history. The default checkout is a
    # single commit, so a job that reads a range of commits needs this.
    checkout =
      {
        ref ? null,
        fetchDepth ? null,
      }:
      {
        uses = "actions/checkout@main";
      }
      // lib.optionalAttrs (ref != null || fetchDepth != null) {
        "with" =
          lib.optionalAttrs (ref != null) { inherit ref; }
          // lib.optionalAttrs (fetchDepth != null) { fetch-depth = fetchDepth; };
      };

    installNix = { }: {
      uses = "cachix/install-nix-action@master";
      "with" = {
        extra_nix_config = "experimental-features = nix-command flakes\n";
      };
    };

    cachix =
      {
        name ? "lillecarl",
      }:
      {
        uses = "cachix/cachix-action@master";
        "with" = {
          inherit name;
          authToken = "\${{ secrets.CACHIX_AUTH_TOKEN }}";
          useDaemon = false;
        };
      };

    enableSandboxNamespaces =
      {
        corePattern ? true,
      }:
      {
        name = "Enable Nix sandbox namespaces";
        run = builtins.concatStringsSep "\n" (
          [
            "sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0"
            "sudo sysctl -w kernel.unprivileged_userns_clone=1"
          ]
          ++ lib.optional corePattern "sudo sysctl -w kernel.core_pattern=/tmp/core.%e.%p"
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
        lib.optionalAttrs (name != null) { inherit name; }
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
