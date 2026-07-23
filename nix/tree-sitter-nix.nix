{
  python,
  treeSitterCli,
  nixpkgsPath,
  treeSitterNixSrc,
}:
let
  # Builds tree-sitter-nix's Python bindings from the tree-sitter-nix-numtide
  # flake input instead of nixpkgs' pinned fetch, so bumping the flake input
  # is all it takes to pick up grammar.js changes -- no round-trip through
  # nixpkgs (bump grammar-sources.nix rev/hash, wait for cache, etc).
  #
  # Points at numtide/tree-sitter-nix, not nix-community/tree-sitter-nix:
  # numtide's README states it's "kept moving while upstream is stalled"
  # and "new bug reports and PRs should be filed here" -- confirmed nixpkgs
  # itself still pins the stalled nix-community rev.
  grammarDrv = treeSitterCli.buildGrammar {
    language = "nix";
    version = "0.0.0-local";
    src = treeSitterNixSrc;
    generate = true;
    # numtide's tree-sitter.json advertises an "ocaml" bindings entry that
    # postdates the tree-sitter-config schema nixpkgs' pinned CLI generates
    # its pydantic model from -- the python wrapper's config() parsing
    # (below) rejects it as an unknown field. Drop just that key; the file
    # is otherwise still valid and still needed (config() uses it to find
    # queries/highlights.scm).
    postPatch = ''
      jq 'del(.bindings.ocaml)' tree-sitter.json > tree-sitter.json.tmp
      mv tree-sitter.json.tmp tree-sitter.json
    '';
  };
in
(python.pkgs.callPackage "${nixpkgsPath}/pkgs/development/python-modules/tree-sitter-grammars" {
  name = "tree-sitter-nix";
  inherit grammarDrv;
}).overridePythonAttrs
  (_: {
    # The builder's own pname is "python-tree-sitter-nix" (drvPrefix =
    # "python-${name}") -- pyproject.toml dependency-name matching
    # (pythonRuntimeDepsCheckHook) needs it to read "tree-sitter-nix",
    # matching the upstream nixpkgs override this replaces.
    pname = "tree-sitter-nix";
  })
