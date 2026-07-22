# pynix-lsp: terranixEntry = import ../default.nix { }
#
# A no-op resource whose only job is to depend on the other two modules'
# resources, demonstrating cross-resource references (resource.other.attr)
# terranix-generated config needs to track for real completion/hover.
{ lib, ... }:
{
  resource.null_resource.demo.triggers = {
    suffix = lib.tfRef "random_id.suffix.hex";
    greeting_path = lib.tfRef "local_file.greeting.filename";
  };
}
