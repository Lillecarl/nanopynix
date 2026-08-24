{ config, lib, ... }:
{
  options.services.example-daemon = {
    enable = lib.mkEnableOption "example daemon";
    port = lib.mkOption {
      type = lib.types.port;
      default = 8080;
      description = "Port the daemon listens on.";
    };
    extraConfig = lib.mkOption {
      type = lib.types.str;
      default = "";
      description = "Extra configuration appended verbatim.";
    };
    secretInternal = lib.mkOption {
      type = lib.types.str;
      default = "";
      internal = true;
      description = "Internal option that must not show up in search results.";
    };
    # A real MyST description, of the shape nixpkgs writes: paragraphs, a Nix
    # code fence with no language tag, and a colon fence. The detail pane of
    # `pynix osearch --tui` renders these through `pynix._markdown`, so the
    # fixture has to carry one or that renderer is never tested on real prose.
    configFiles = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = ''
        Files that the daemon reads when it starts.

        Each entry is an absolute path. The daemon reads the files in order,
        and a later file overrides an earlier one.

        ```
        services.example-daemon.configFiles = [ "/etc/example.conf" ];
        ```

        ::: {.note}
        The daemon does not watch these files. Restart it after a change.
        :::
      '';
    };
    # `readOnly` reaches `OptionRecord.read_only`, and the detail pane marks it.
    stateVersion = lib.mkOption {
      type = lib.types.str;
      default = "24.05";
      readOnly = true;
      description = "The release that this daemon state belongs to.";
    };
    # Mirrors real-world modules (e.g. disko) whose defaults are expressions
    # over `config` that only resolve once a whole system is realized --
    # evaluating this option's default in isolation throws "attribute
    # 'doesNotExist' missing". Indexing must survive this without aborting.
    brokenDefault = lib.mkOption {
      type = lib.types.str;
      default = config.services.example-daemon.doesNotExist;
      description = "An option whose default cannot be evaluated in isolation.";
    };
  };
}
