// Command tofu-core-schema exports OpenTofu's built-in ("core") HCL block
// schema -- resource/data/variable/output/module/provider/terraform/locals
// and their meta-arguments (count, for_each, depends_on, lifecycle, ...) --
// as JSON, for a given OpenTofu version.
//
// This is the schema OpenTofu itself understands regardless of which
// provider is in play, as opposed to `tofu providers schema -json`'s
// per-provider resource/data-source attribute schemas. pynix's terranix LSP
// dialect merges both so hover/completion works on `count`, `for_each`,
// `lifecycle { ... }`, etc. (core schema) as well as `byte_length`,
// `keepers`, etc. (provider schema).
//
// The output shape deliberately mirrors `tofu providers schema -json`'s own
// per-resource "block" shape (`attributes` + `block_types`, each block
// wrapping a further nested `block`), so pynix's existing
// `_terranix_schema.py` SchemaBlock/SchemaAttribute models and
// walk_schema_block/render_schema_attribute helpers work unmodified against
// either source.
package main

import (
	"encoding/json"
	"fmt"
	"os"

	goversion "github.com/hashicorp/go-version"
	hclschema "github.com/hashicorp/hcl-lang/schema"
	tofuschema "github.com/opentofu/opentofu-schema/schema"
)

type exportedAttribute struct {
	Type        string `json:"type,omitempty"`
	Description string `json:"description,omitempty"`
	Required    bool   `json:"required,omitempty"`
	Optional    bool   `json:"optional,omitempty"`
	Computed    bool   `json:"computed,omitempty"`
	Deprecated  bool   `json:"deprecated,omitempty"`
}

type exportedBlockBody struct {
	Attributes map[string]exportedAttribute   `json:"attributes,omitempty"`
	BlockTypes map[string]exportedNestedBlock `json:"block_types,omitempty"`
}

type exportedNestedBlock struct {
	Block exportedBlockBody `json:"block"`
}

type exportedBlock struct {
	Labels      []string          `json:"labels,omitempty"`
	Description string            `json:"description,omitempty"`
	Block       exportedBlockBody `json:"block"`
}

// safeFriendlyName calls Constraint.FriendlyName(), recovering from a panic.
//
// hcl-lang's AnyExpression/LiteralType.FriendlyName() call
// cty.Type.FriendlyNameForConstraint() on their OfType/Type field, which
// panics on cty.Type{}'s zero value (an unconstrained "any" type, used
// throughout OpenTofu's core schema for things like `default`'s value or
// `postcondition`'s condition expression) -- confirmed by tofu-core-schema
// panicking on the "resource" block's `lifecycle.postcondition.condition`
// attribute without this guard. Recovering and falling back to "any" is
// correct here, not a workaround: an unconstrained AnyExpression really does
// mean "any expression", which "any" describes precisely.
func safeFriendlyName(c hclschema.Constraint) (name string) {
	defer func() {
		if recover() != nil {
			name = "any"
		}
	}()
	return c.FriendlyName()
}

// extensionAttributes synthesizes pseudo-attributes for the count/for_each
// meta-arguments hcl-lang's decoder handles specially rather than declaring
// as ordinary AttributeSchema entries: BodySchema.Extensions.Count/ForEach
// are plain bools with no accompanying schema at all (confirmed against
// opentofu-schema's resource_block.go, which sets Extensions.Count/ForEach
// instead of an Attributes["count"]/["for_each"] entry) -- so without this,
// `count`/`for_each` would silently never appear in exported schema despite
// being real, universally completable arguments on every resource/data/
// module block.
func extensionAttributes(bs *hclschema.BodySchema) map[string]exportedAttribute {
	if bs == nil || bs.Extensions == nil {
		return nil
	}
	attrs := map[string]exportedAttribute{}
	if bs.Extensions.Count {
		attrs["count"] = exportedAttribute{
			Type:        "number",
			Description: "The number of instances of this block to create.",
			Optional:    true,
		}
	}
	if bs.Extensions.ForEach {
		attrs["for_each"] = exportedAttribute{
			Type:        "map or set of any",
			Description: "A map or set of strings/objects to create one instance of this block per element.",
			Optional:    true,
		}
	}
	return attrs
}

func convertBody(bs *hclschema.BodySchema) exportedBlockBody {
	body := exportedBlockBody{}
	if bs == nil {
		return body
	}
	extensionAttrs := extensionAttributes(bs)
	if len(bs.Attributes) > 0 || len(extensionAttrs) > 0 {
		body.Attributes = make(map[string]exportedAttribute, len(bs.Attributes)+len(extensionAttrs))
		for name, attr := range bs.Attributes {
			exported := exportedAttribute{
				Description: attr.Description.Value,
				Required:    attr.IsRequired,
				Optional:    attr.IsOptional,
				Computed:    attr.IsComputed,
				Deprecated:  attr.IsDeprecated,
			}
			if attr.Constraint != nil {
				exported.Type = safeFriendlyName(attr.Constraint)
			}
			body.Attributes[name] = exported
		}
		for name, attr := range extensionAttrs {
			body.Attributes[name] = attr
		}
	}
	if len(bs.Blocks) > 0 {
		body.BlockTypes = make(map[string]exportedNestedBlock, len(bs.Blocks))
		for name, block := range bs.Blocks {
			body.BlockTypes[name] = exportedNestedBlock{Block: convertBody(block.Body)}
		}
	}
	return body
}

func main() {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "usage: tofu-core-schema <tofu-version>")
		os.Exit(2)
	}

	v, err := goversion.NewVersion(os.Args[1])
	if err != nil {
		fmt.Fprintf(os.Stderr, "invalid version %q: %s\n", os.Args[1], err)
		os.Exit(1)
	}

	bs, err := tofuschema.CoreModuleSchemaForVersion(v)
	if err != nil {
		fmt.Fprintf(os.Stderr, "no core schema for version %q: %s\n", os.Args[1], err)
		os.Exit(1)
	}

	out := make(map[string]exportedBlock, len(bs.Blocks))
	for name, block := range bs.Blocks {
		labels := make([]string, len(block.Labels))
		for i, label := range block.Labels {
			labels[i] = label.Name
		}
		out[name] = exportedBlock{
			Labels:      labels,
			Description: block.Description.Value,
			Block:       convertBody(block.Body),
		}
	}

	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(out); err != nil {
		fmt.Fprintf(os.Stderr, "encode: %s\n", err)
		os.Exit(1)
	}
}
