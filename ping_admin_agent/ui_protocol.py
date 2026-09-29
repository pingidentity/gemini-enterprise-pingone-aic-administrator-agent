"""A2UI 0.9 catalog negotiation and offline validation of outgoing surfaces."""

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from google.protobuf.json_format import MessageToDict
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from .a2ui import BASIC_CATALOG_ID, EXTENSION_URI

WIRE_VERSION = "v0.9"
GEMINI_CATALOG_ID = "https://www.gstatic.com/vertexaisearch/a2ui/v0_9/gemini_enterprise_composite_catalog.json"
SUPPORTED_CATALOG_IDS = (GEMINI_CATALOG_ID, BASIC_CATALOG_ID)


def ui_enabled() -> bool:
    value = os.getenv("PING_ADMIN_A2UI_ENABLED", "true")
    return value.lower().strip() in {"true", "1", "yes"}


def panel_enabled() -> bool:
    """Root composite-catalog workspaces in Gemini Enterprise's side panel."""
    value = os.getenv("PING_ADMIN_A2UI_PANEL", "true")
    return value.lower().strip() in {"true", "1", "yes"}


def canvas_logo_enabled() -> bool:
    """Image component inside the rendered workspace.

    Gemini Enterprise's MaterialImage showed a broken image for external URLs
    (tested 2026-09-17 against pingidentity.com, *.run.app and
    assets.pingone.com PNG/SVG assets). The logo is now the basic Image
    component, which is reported to load such URLs; it stays opt-in until that
    is confirmed in Gemini Enterprise. The A2A card's iconUrl brands the
    registration either way.
    """
    value = os.getenv("PING_ADMIN_CANVAS_LOGO", "false")
    return value.lower().strip() in {"true", "1", "yes"}


def _catalog_from_capabilities(capabilities: Any) -> str | None:
    """Read supportedCatalogIds from either capability shape.

    Clients wrap the catalog list in a version key ({"v0.9": {...}}) per the
    extension spec, but Gemini Enterprise sends it flat at request level
    ({"supportedCatalogIds": [...]}). Both spellings are accepted; the catalogs
    themselves stay restricted to SUPPORTED_CATALOG_IDS.
    """
    if not isinstance(capabilities, dict):
        return None
    version = capabilities.get(WIRE_VERSION) or capabilities.get("v0.9.1")
    if isinstance(version, dict):
        candidates = version.get("supportedCatalogIds")
    else:
        candidates = capabilities.get("supportedCatalogIds")
    if not isinstance(candidates, list) or not all(
        isinstance(item, str) for item in candidates
    ):
        return None
    return next((item for item in SUPPORTED_CATALOG_IDS if item in candidates), None)


def _client_capabilities(context) -> Any:
    """The client's a2uiClientCapabilities from the message or the request.

    The extension spec places the object in message.metadata; Gemini Enterprise
    sends it in the request-level metadata instead. The first location that
    carries the key wins.
    """
    message = MessageToDict(context.message.metadata)
    if "a2uiClientCapabilities" in message:
        return message["a2uiClientCapabilities"]
    request_metadata = getattr(context, "metadata", None)
    if (
        isinstance(request_metadata, dict)
        and "a2uiClientCapabilities" in request_metadata
    ):
        return request_metadata["a2uiClientCapabilities"]
    return None


def surface_mode() -> str:
    """conversation: one side-panel surface per conversation, refreshed in place.

    turn: a new surface, and therefore a new opener card and panel entry, for
    every response. The Basic catalog and inline panels are always per turn.
    """
    value = os.getenv("PING_ADMIN_A2UI_SURFACE", "conversation").lower().strip()
    return value if value in {"conversation", "turn"} else "conversation"


def negotiate_catalog(context) -> str | None:
    if not ui_enabled():
        return None
    capabilities = _client_capabilities(context)
    if capabilities is not None:
        # A client that states its catalogs gets one of them or plain text.
        # The header-only fallback below is for clients that state nothing.
        return _catalog_from_capabilities(capabilities)
    requested = set(context.requested_extensions or []) | set(
        context.message.extensions
    )
    return BASIC_CATALOG_ID if EXTENSION_URI in requested else None


def _alias_missing_defs(catalog: dict, common: dict) -> dict:
    """Resolve a catalog's dangling local $defs pointers against common types.

    Google's composite catalog references "#/$defs/ChildList" from MaterialTabs
    and MaterialTab although it only defines ChildList in common_types.json.
    Validating any component walks those branches, so without an alias the
    validator aborts instead of reporting a result. The snapshot on disk stays
    unmodified; only the in-memory validation resource gains the aliases.
    """
    missing = set()

    def walk(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                missing.add(ref.split("/")[-1])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(catalog)
    defs = dict(catalog.get("$defs", {}))
    for name in sorted(missing - set(defs)):
        if name in common.get("$defs", {}):
            defs[name] = {"$ref": f"{common['$id']}#/$defs/{name}"}
    return {**catalog, "$defs": defs}


@lru_cache(maxsize=2)
def validator(catalog_id: str) -> Draft202012Validator:
    if catalog_id not in SUPPORTED_CATALOG_IDS:
        raise ValueError("Unsupported UI catalog")
    directory = Path(__file__).parent / "schemas"
    server, common, catalog = [
        json.loads((directory / name).read_text())
        for name in (
            "server_to_client.json",
            "common_types.json",
            "gemini_enterprise_v09.json"
            if catalog_id == GEMINI_CATALOG_ID
            else "catalog.json",
        )
    ]
    catalog = _alias_missing_defs(catalog, common)
    resources = [
        (item["$id"], Resource.from_contents(item))
        for item in (server, common, catalog)
    ]
    resources.extend(
        [
            (
                "https://a2ui.org/specification/v0_9/catalog.json",
                Resource.from_contents(catalog),
            ),
            (
                "https://a2ui.org/specification/v0_9/common_types.json",
                Resource.from_contents(common),
            ),
        ]
    )
    return Draft202012Validator(server, registry=Registry().with_resources(resources))


def validate_surface(
    messages: list[dict], catalog_id: str, *, partial: bool = False
) -> None:
    """Validate outgoing messages against the schemas and our lifecycle rules.

    ``partial`` allows an updateComponents message that redefines a few
    components of a surface the client already holds: it needs no root, and it
    may reference components sent by an earlier update.
    """
    for message in messages:
        validator(catalog_id).validate(message)
    updates = [m["updateComponents"] for m in messages if "updateComponents" in m]
    for update in updates:
        components = update["components"]
        ids = {component["id"] for component in components}
        if len(ids) != len(components) or not (partial or "root" in ids):
            raise ValueError("A surface needs a unique root and unique component IDs")
        if partial:
            continue
        for component in components:
            children = component.get("children", [])
            refs = children if isinstance(children, list) else [children["componentId"]]
            refs += [component["child"]] if "child" in component else []
            refs += [
                tab.get("child", tab.get("content"))
                for tab in component.get("tabs", [])
            ]
            if any(ref not in ids for ref in refs):
                raise ValueError("Unknown child component")
