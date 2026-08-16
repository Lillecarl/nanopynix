# pynix-lsp: moduleEntry = { _module = { args = { testArg = "fromModuleArgs"; onlyInArgs = "onlyArgsValue"; }; specialArgs = { testArg = "fromSpecialArgs"; onlyInSpecialArgs = "onlySpecialValue"; notDeclared = "shouldNotResolve"; }; }; }
#
# A synthetic, non-evalModules moduleEntry -- exercises `resolve_module_arg`'s
# `_module.specialArgs` vs `_module.args` precedence directly (nixpkgs'
# `lib/modules.nix` `applyModuleArgs` resolves each formal as `args.${name}
# or config._module.args.${name}`, where `args` is built from `specialArgs`,
# so specialArgs wins on a name present in both) without paying for a real
# module-system eval. `notDeclared` exists in `_module.specialArgs` but is
# deliberately NOT one of this file's own lambda formals below, to prove
# module-arg completion is gated on what the file itself actually declares.
{ config, testArg, onlyInArgs, onlyInSpecialArgs, ... }:
{
  _unused = [ testArg onlyInArgs onlyInSpecialArgs notDeclared ];
}
