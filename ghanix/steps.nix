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

  # Put a cap on one step. `schema.nix` gives the reason a step carries its
  # own cap beside the cap of the job.
  withTimeout = minutes: attrs: attrs // { timeout-minutes = minutes; };

  # Each constructor below carries a default cap, because each one wraps a
  # known action whose work does not change with the project that calls it: a
  # checkout, an install of Nix, a read or a write of one artifact. Each
  # default is generous against the measured time, because the cap is there to
  # catch a step that stopped and not a step that is slow. A caller that knows
  # better passes `timeoutMinutes`.
  steps = {
    # `fetchDepth = 0` fetches the whole history. The default checkout is a
    # single commit, so a job that reads a range of commits needs this.
    checkout =
      {
        ref ? null,
        fetchDepth ? null,
        timeoutMinutes ? 10,
      }:
      {
        uses = "actions/checkout@main";
        timeout-minutes = timeoutMinutes;
      }
      // lib.optionalAttrs (ref != null || fetchDepth != null) {
        "with" =
          lib.optionalAttrs (ref != null) { inherit ref; }
          // lib.optionalAttrs (fetchDepth != null) { fetch-depth = fetchDepth; };
      };

    installNix =
      {
        timeoutMinutes ? 15,
      }:
      {
        uses = "cachix/install-nix-action@master";
        timeout-minutes = timeoutMinutes;
        "with" = {
          extra_nix_config = "experimental-features = nix-command flakes\n";
        };
      };

    cachix =
      {
        name ? "lillecarl",
        timeoutMinutes ? 15,
      }:
      {
        uses = "cachix/cachix-action@master";
        timeout-minutes = timeoutMinutes;
        "with" = {
          inherit name;
          authToken = "\${{ secrets.CACHIX_AUTH_TOKEN }}";
          useDaemon = false;
        };
      };

    # There is no `enableSandboxNamespaces` here any more. It was the one
    # constructor in this file whose `run` body was a script rather than a
    # command, and a body that a workflow file carries is read by no gate and
    # runs on no machine but a runner. It is
    # `ci/steps.nix`'s `enable-sandbox-namespaces` now, and
    # `ci/workflows/lib.nix` calls it through `mkSandboxStep`. Every
    # constructor left here either names an action or runs one command.

    verifyClosure =
      {
        name,
        timeoutMinutes ? 15,
      }:
      {
        inherit name;
        timeout-minutes = timeoutMinutes;
        run = ''nix store verify --recursive --no-trust "$(readlink -f result)"'';
      };

    uploadArtifact =
      {
        name ? null,
        artifactName,
        path,
        cond ? "\${{ !cancelled() }}",
        timeoutMinutes ? 15,
      }:
      withCond cond (
        lib.optionalAttrs (name != null) { inherit name; }
        // {
          uses = "actions/upload-artifact@main";
          timeout-minutes = timeoutMinutes;
          "with" = {
            name = artifactName;
            inherit path;
          };
        }
      );

    downloadArtifact =
      {
        artifactName,
        timeoutMinutes ? 10,
      }:
      {
        uses = "actions/download-artifact@main";
        timeout-minutes = timeoutMinutes;
        "with" = {
          name = artifactName;
        };
      };
  };
}
