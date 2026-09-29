# Ping Identity branding assets

`ping-identity-logo.svg` is the unmodified official horizontal color logo used
in the header of [Ping Identity's website](https://www.pingidentity.com/en.html).

- [Original SVG](https://www.pingidentity.com/content/dam/picr/nav/Logo-Ping-Brand-Horizontal-Color.svg)
- Retrieved: September 8, 2026.
- Original SVG SHA-256: `9f6f6f4a903f4dad22b9cba300225185c6d5c4b7b33d4be4299100458c743d60`.
- Original dimensions: 152 × 26.
- `ping-identity-logo.png` renders that SVG at 608 × 104 on white, preserving
  its proportions, artwork, and colors. It is provided for image uploads and
  Gemini Enterprise's base64 `icon.content` field.

These bundled files serve Gemini Enterprise's registration `icon` field. The
A2A `iconUrl` and the A2UI panel header default to Ping Identity's
[square mark](https://www.pingidentity.com/content/dam/ping-6-2-assets/topnav-json-configs/Ping-Logo.svg),
which is loaded from Ping Identity's website at run time and is not bundled here.

`ping-logo-square-1251.png` is the brand team's square mark
([PIC-Square-Logo-Primary-2026.png](../../../logos/PIC-Square-Logo-Primary-2026.png),
1251 × 1251, white on red), the same mark the PingID Device Management agent
shows as its small circular avatar in Gemini Enterprise. The registration
payload `gemini-enterprise-icon.json` references the public HTTPS copy of this
mark through `icon.uri` rather than embedding bytes.

The logo belongs to Ping Identity. It is a third-party brand asset, not artwork
created by this project. Keep its proportions and colors when displaying it.
