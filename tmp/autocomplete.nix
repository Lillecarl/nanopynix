let
  drv = builtins.derivation {};
in
{
  # pynix build --file ./tmp/autocomplete.nix#nixo<tab> should resolve to nixos
  # pynix build --file ./tmp/autocomplete.nix --attr nixo<tab> should resolve to nixos
  nixos = {
    _type = "attrs";
    class = "nixos";
    config.system.built.toplevel = drv;
  };
}
