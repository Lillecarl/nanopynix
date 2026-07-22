# tofu-core-schema

Exports OpenTofu's built-in ("core") HCL block schema -- `resource`, `data`,
`variable`, `output`, `module`, `provider`, `terraform`, `locals`, and their
meta-arguments (`count`, `for_each`, `depends_on`, `lifecycle { ... }`, ...) --
as JSON, sourced from [opentofu-schema](https://github.com/opentofu/opentofu-schema)
(the schema library terraform-ls/opentofu-ls themselves use).

`package.nix` builds this as an ordinary Go package (`tofuCoreSchemaTool` in
`../../default.nix`). Nothing here is hand-run or committed to git:
`pynix/src/pynix/_lsp/_tofu_core_schema.py` invokes the built binary live, at
LSP-server runtime, with whatever exact OpenTofu version it detected
(`tofu version -json`) -- unlike the provider schema (`tofu providers schema
-json`, see `_terranix_schema.py`), the core schema needs nothing
project-specific, so there's nothing to gain from pre-baking a snapshot: the
subprocess call is a cheap, deterministic, in-process-memory-cached
computation, not a network fetch.

The tool is put on `PATH` for both the real build (`pynix/package.nix`'s
`makeWrapperArgs`) and the editable-install dev shell (`nix/shell.nix`'s
`packages`), so Go itself is only ever a build-time dependency of this one
package -- cached by Nix like any other content-addressed build, never
required at LSP-server runtime.

## Usage

```console
$ nix run .#tofuCoreSchemaTool -- 1.12.4
```

The argument is any OpenTofu version string; `opentofu-schema`'s own
`CoreModuleSchemaForVersion` picks the closest version-cascaded schema at or
below it.

## Output shape

Deliberately mirrors `tofu providers schema -json`'s own per-resource `block`
shape (`attributes` + `block_types`, each nested block wrapping a further
`block`), so `pynix`'s existing `_terranix_schema.py` `SchemaBlock`/
`SchemaAttribute` models and `walk_schema_block`/`list_block_attributes`/
`render_schema_attribute` helpers work against either source unmodified:

```json
{
  "resource": {
    "labels": ["type", "name"],
    "description": "...",
    "block": {
      "attributes": {
        "count": {"type": "number", "description": "...", "optional": true}
      },
      "block_types": {
        "lifecycle": {"block": {"attributes": {"prevent_destroy": {...}}}}
      }
    }
  }
}
```

`count`/`for_each` are synthesized (see `extensionAttributes` in `main.go`):
hcl-lang's decoder handles them via plain `BodySchema.Extensions.Count`/
`ForEach` booleans rather than ordinary attribute schema entries, so without
this they'd silently never appear in the exported JSON despite being real,
universally-completable resource/data/module arguments.
