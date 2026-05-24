{ config, pkgs, lib, ... }:

let
  inherit (lib) mkIf mkOption types literalExpression;
  cfg = config.services.pynixd;

  mergedSettings = {
    unix_path = "/run/pynixd/pynixd.sock";
  } // cfg.settings;

  configFile = pkgs.writeText "pynixd.json" (builtins.toJSON mergedSettings);
in
{
  options.services.pynixd = {
    enable = mkOption {
      type = types.bool;
      default = false;
      description = "Enable pynixd, a Python Nix daemon protocol proxy.";
    };

    package = mkOption {
      type = types.package;
      description = "The pynixd package to use.";
      example = literalExpression "pkgs.pynixd";
    };

    settings = mkOption {
      type = types.attrs;
      default = { };
      description = ''
        Extra settings serialized to JSON and passed to pynixd via PYNIXD_CONFIG.
        See PynixdSettings in pynixd/config.py for the full list.
        Defaults: { unix_path = "/run/pynixd/pynixd.sock" }
      '';
      example = literalExpression ''
        {
          stores = [
            { type = "ssh-subprocess"; host = "builder1"; systems = [ "x86_64-linux" ]; }
          ];
          schedule_mode = "auto";
        }
      '';
    };
  };

  config = mkIf cfg.enable {
    systemd.services.pynixd = {
      description = "pynixd - Python Nix daemon protocol proxy";
      wantedBy = [ "multi-user.target" ];
      after = [ "nix-daemon.service" ];
      wants = [ "nix-daemon.service" ];

      serviceConfig = {
        Type = "simple";
        ExecStart = "${lib.getExe cfg.package}";
        User = "root";
        Group = "root";
        RuntimeDirectory = "pynixd";
        RuntimeDirectoryMode = "755";
        Environment = "PYNIXD_CONFIG=${configFile}";
        Restart = "on-failure";
        RestartSec = "5s";
        PrivateTmp = true;
        NoNewPrivileges = true;
      };
    };

    environment.systemPackages = [ cfg.package ];
  };
}
