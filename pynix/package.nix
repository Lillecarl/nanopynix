{
  lib,
  buildPythonApplication,
  nanopynix,
  clypi,
  python,
  renderPyproject,
  tofuCoreSchemaTool,
  treeSitterCli,
  nixpkgsPath,
}:
let
  attrs = renderPyproject {
    projectRoot = ./.;
    inherit python;
    pythonPackages = python.pkgs // {
      inherit nanopynix clypi;
      "tree-sitter-nix" = import ../nix/tree-sitter-nix.nix { inherit python treeSitterCli nixpkgsPath; };
    };
  };
in
buildPythonApplication (
  attrs
  // {

    src = lib.cleanSource ./.;

    # pynix._lsp._tofu_core_schema invokes tools/tofu-core-schema's binary at
    # LSP-server runtime (see its module docstring) rather than baking a
    # static snapshot -- put it on the wrapped executable's PATH so a plain
    # `nanopynix-tofu-core-schema <version>` subprocess call resolves without
    # needing an absolute store path threaded through Python.
    makeWrapperArgs = [
      "--prefix"
      "PATH"
      ":"
      (lib.makeBinPath [ tofuCoreSchemaTool ])
    ];

    meta = attrs.meta // {
      platforms = lib.platforms.unix;
    };
  }
)
