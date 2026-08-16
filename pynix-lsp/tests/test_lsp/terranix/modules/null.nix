# pynix-lsp: terranixEntry = import ../default.nix { }
#
# A no-op resource whose only job is to depend on the other two modules'
# resources, demonstrating cross-resource references (resource.other.attr)
# terranix-generated config needs to track for real completion/hover.
#
# Also declares `pkgs` (unlike the other modules here, which only need
# `lib`) to exercise `_module.args` resolution for a terranix file's own
# top-level lambda formals -- see ../default.nix's `moduleSystem` and
# _terranix.py's module docstring.
{ lib, pkgs, ... }:
{
  resource.null_resource.demo.triggers = {
    suffix = lib.tfRef "random_id.suffix.hex";
    greeting_path = lib.tfRef "local_file.greeting.filename";
  };

#                                                  vLSPOINT9"s"
  locals.build_platform = pkgs.stdenv.hostPlatform.system;
}
