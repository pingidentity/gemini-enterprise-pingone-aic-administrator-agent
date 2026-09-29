# A2UI 0.9 schemas

Unmodified snapshots of the published schemas, retrieved and verified against
the live URLs on September 15, 2026. Validation runs offline with a fixed
registry; client-provided catalogs cannot trigger schema downloads.

- [Server protocol](https://a2ui.org/specification/v0_9/json/server_to_client.json)
- [Common types](https://a2ui.org/specification/v0_9/json/common_types.json)
- [Basic catalog](https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json)
- [Gemini Enterprise composite catalog](https://www.gstatic.com/vertexaisearch/a2ui/v0_9/gemini_enterprise_composite_catalog.json)

Composite catalog SHA-256: `836d79eaffc603e6bd6f5c79fee28acd0867dc173762611b61da274503d74f3c`.
The composite catalog's `MaterialTabs` and `MaterialTab` entries reference
`#/$defs/ChildList`, which the catalog does not define; the offline validator
aliases such dangling local pointers to `common_types.json` at load time and
leaves this snapshot unmodified.
Google updates the composite catalog in place; download it again and compare
the checksum before each release, because a newer catalog can add or change
Material component properties. The basic/protocol schemas use the included
Apache-2.0 license. See Google's
[component reference](https://docs.cloud.google.com/gemini/enterprise/docs/a2ui-agents/a2ui-component-gallery-reference)
for the Material components. Passing schemas does not establish native Gemini
rendering or accessibility; validate those in the customer's app.
