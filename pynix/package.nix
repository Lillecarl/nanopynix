{
  lib,
  buildPythonApplication,
  nanopynix,
  python,
  renderPyproject,
  tofuCoreSchemaTool,
  # Optional: pynix/pyproject.toml's `ekn` extra. Deliberately not resolved
  # through `renderPyproject`'s own dependency graph (pynix's
  # [project.dependencies] never lists ekn) -- ekn's own build depends on
  # nanopynix, so pynix depending on ekn at the wheel-metadata level would
  # create a derivation cycle. Bundled here into the final environment
  # instead; pynix/src/pynix/__init__.py imports it via a guarded
  # try/except so the build still works with `ekn = null`.
  ekn ? null,
}:
let
  attrs = renderPyproject {
    projectRoot = toString ./.;
    inherit python;
  };
in
buildPythonApplication (
  attrs
  // {

    src = ./.;

    dependencies = attrs.dependencies ++ lib.optionals (ekn != null) [ ekn ];

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
