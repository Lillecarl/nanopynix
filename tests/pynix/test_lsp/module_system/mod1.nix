{
  config,
  pkgs,
  lib,
  ...
}:
{
  options = {
    programs.example = {
      enable = lib.mkEnableOption "example";
      package = lib.mkOption {
        type = lib.types.package;
        default = pkgs.hello;
        description = "Package providing the `example` program.";
      };
      settings = lib.mkOption {
        type = lib.types.attrsOf lib.types.anything;
        default = { };
        description = "Freeform settings written to the example program's config file.";
        example = {
          greeting = "hi";
        };
      };
    };
  };
}
