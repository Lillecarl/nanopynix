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
