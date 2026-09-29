"""Generate the Google Cloud Marketplace Agent Card from the deployed card.

The card is stored as a JSON file in a Cloud Storage bucket in the
Marketplace producer project (forgerock-public; convention:
gemini-enterprise-agent-<agent-slug>/agent-card.json, private). The shape
that Google validated for Ping's published PingID listing is the A2A 0.3
wire spelling (url, preferredTransport, protocolVersion) with provider:
so this card keeps the served card's shape rather than translating to the
1.0 spelling.

Customers deploy the agent manually into their own project and receive
their own runtime URL, so the published card has no single operational
endpoint; its interface URL names the listing page and must be confirmed
with the Google partner engineer before Save and validate.

Usage::

    python -m scripts.marketplace_card \
        --source deployment-output/agent-card.json \
        --out marketplace/agent-card.json
"""

from __future__ import annotations

import argparse
import json

# Where customers learn about and start the deployment; the interface URL is
# a placeholder naming the product, not an A2A endpoint, because customers
# deploy manually into their own project.
INTERFACE_URL = (
    "https://marketplace.pingone.com/item/"
    "google-gemini-enterprise-pingone-aic-administrator-agent"
)


def marketplace_card(source: dict) -> dict:
    """The served 0.3-spelled card with the listing URL swapped in."""
    card = dict(source)
    card["url"] = INTERFACE_URL
    return card


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=argparse.FileType("r"), required=True)
    parser.add_argument("--output", type=argparse.FileType("w"), required=True)
    args = parser.parse_args()
    card = marketplace_card(json.load(args.source))
    json.dump(card, args.output, indent=2)
    args.output.write("\n")
    print(f"Marketplace card written: {args.output.name}")


if __name__ == "__main__":
    main()

