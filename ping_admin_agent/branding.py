"""Shared Ping Identity branding for agent discovery, UI, and registration."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

# The square mark from Ping Identity's site navigation: white text on a solid
# red square, so it reads in light and dark themes and suits icon slots. It is
# loaded from Ping Identity's website at run time and is not bundled.
OFFICIAL_LOGO_URL = (
    "https://www.pingidentity.com/content/dam/ping-6-2-assets/"
    "topnav-json-configs/Ping-Logo.svg"
)
# Where the bundled horizontal logo files under assets/ were retrieved from.
BUNDLED_LOGO_SOURCE_URL = (
    "https://www.pingidentity.com/content/dam/picr/nav/"
    "Logo-Ping-Brand-Horizontal-Color.svg"
)
ASSET_DIRECTORY = Path(__file__).with_name("assets")
LOGO_SVG = ASSET_DIRECTORY / "ping-identity-logo.svg"
LOGO_PNG = ASSET_DIRECTORY / "ping-identity-logo.png"
# The brand team's square mark (logos/PIC-Square-Logo-Primary-2026.png), the
# same artwork the PingID Device Management agent advertises and Gemini
# Enterprise shows as its small circular avatar. Bundled so this agent's
# registration icon replaces the default initial-letter avatar with it.
SQUARE_LOGO_PNG = ASSET_DIRECTORY / "ping-logo-square-1251.png"


def logo_url() -> str:
    """Use the official asset, or a customer's public HTTPS copy of that asset."""
    value = os.environ.get("PING_ADMIN_LOGO_URL", "").strip() or OFFICIAL_LOGO_URL
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError(
            "PING_ADMIN_LOGO_URL must be a public HTTPS URL without credentials"
        )
    return value
