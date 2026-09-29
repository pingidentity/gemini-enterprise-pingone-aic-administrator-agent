"""Write a portable manual registration bundle without changing cloud resources."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from ping_admin_agent.branding import (
    ASSET_DIRECTORY,
    BUNDLED_LOGO_SOURCE_URL,
    LOGO_PNG,
    LOGO_SVG,
    SQUARE_LOGO_PNG,
)


def write_registration_bundle(output_dir: Path, card: dict, runtime: dict) -> Path:
    """Package the verified live card, runtime reference, logo, and icon patch.

    Entries are explicitly enumerated so unrelated files (including credentials)
    in the customer's output directory can never enter the ZIP.
    """
    if not card.get("iconUrl"):
        raise ValueError("The live card has no branding icon; update the runtime first")
    png = LOGO_PNG.read_bytes()
    svg = LOGO_SVG.read_bytes()
    square_png = SQUARE_LOGO_PNG.read_bytes()

    def as_json(value: dict) -> bytes:
        return (json.dumps(value, indent=2) + "\n").encode("utf-8")

    files = {
        "agent-card.json": as_json(card),
        "runtime.json": as_json(runtime),
        # Gemini Enterprise renders the registration avatar from icon.uri; the
        # inline base64 content form is stored but the UI still shows the
        # default initial-letter avatar (verified live 2026-09-25). The card's
        # iconUrl is the customer's own public HTTPS copy of the official mark.
        "gemini-enterprise-icon.json": as_json(
            {
                "icon": {"uri": card["iconUrl"]},
            }
        ),
        "branding.json": as_json(
            {
                "displayName": card["name"],
                # Provenance of the bundled files, not the runtime default.
                "logoSourceUri": BUNDLED_LOGO_SOURCE_URL,
                "a2aIconUrl": card["iconUrl"],
                "geminiEnterpriseIconUpdateMask": "icon",
                "assets": {
                    "assets/ping-identity-logo.svg": {
                        "sha256": hashlib.sha256(svg).hexdigest()
                    },
                    "assets/ping-identity-logo.png": {
                        "sha256": hashlib.sha256(png).hexdigest()
                    },
                    "assets/ping-logo-square-1251.png": {
                        "sha256": hashlib.sha256(square_png).hexdigest()
                    },
                },
            }
        ),
        "assets/ping-identity-logo.svg": svg,
        "assets/ping-identity-logo.png": png,
        "assets/ping-logo-square-1251.png": square_png,
        "assets/README.md": (ASSET_DIRECTORY / "README.md").read_bytes(),
        "REGISTRATION.md": Path(__file__)
        .with_name("registration-instructions.md")
        .read_bytes(),
    }
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        path = output_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    bundle_path = output_dir / "manual-registration.zip"
    with ZipFile(bundle_path, "w", compression=ZIP_DEFLATED) as bundle:
        for name, content in files.items():
            bundle.writestr(name, content)
    return bundle_path
