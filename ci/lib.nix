# Minimal function+attrset library for building GitHub Actions workflows as
# plain Nix values, rendered to YAML via nanopynix's toYAML primop (see
# ci/render.py). Deliberately not a NixOS-module-based system yet -- this is
# the "get something working" first step; module-system ergonomics (option
# declarations, merging, etc.) can replace these plain functions later
# without changing the workflow definitions' shape much.
#
# No nixpkgs `lib` dependency on purpose: workflow definitions should
# evaluate hermetically from a plain `nix-instantiate --eval`/`eval_.file()`
# call, without needing a working NIX_PATH.
rec {
  optionalAttrs = cond: attrs: if cond then attrs else { };
  optional = cond: x: if cond then [ x ] else [ ];

  mkWorkflow =
    {
      name,
      on,
      jobs,
    }:
    {
      inherit name on jobs;
    };

  # Job attrs are passed through as-is other than defaulting runs-on --
  # every job field GitHub Actions understands (if, needs, outputs,
  # strategy, permissions, environment, concurrency, steps, ...) is just
  # forwarded.
  mkJob =
    { runsOn ? "ubuntu-24.04", ... }@args:
    (removeAttrs args [ "runsOn" ]) // { runs-on = runsOn; };

  # `if` is a Nix keyword, so it can't be a formal-argument name -- callers
  # pass the condition as `cond` and this helper renders it under the
  # literal (quoted) attribute name "if". Works for both step attrs and
  # job attrs.
  withCond = cond: attrs: if cond == null then attrs else attrs // { "if" = cond; };

  steps = {
    checkout =
      { ref ? null }:
      { uses = "actions/checkout@main"; } // optionalAttrs (ref != null) { "with" = { inherit ref; }; };

    nixQuickInstall = { }: { uses = "nixbuild/nix-quick-install-action@master"; };

    installNixMultiUser = { }: {
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

    configureSingleUserNix = { }: {
      name = "Configure single-user Nix builds";
      run = # bash
        ''
          sudo install -d -m 0755 /etc/nix
          printf '%s\n' 'build-users-group =' 'require-drop-supplementary-groups = false' | sudo tee -a /etc/nix/nix.conf
        '';
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
