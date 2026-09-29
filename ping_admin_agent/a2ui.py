"""A2UI v0.9 wire constants shared by the agent card, executor and UI protocol.

Google's A2UI SDK emits the ``application/json+a2ui`` MIME spelling and the
``.../a2ui/v0.9`` extension URI for v0.9 clients such as Gemini Enterprise;
later protocol versions switched to ``application/a2ui+json``.
"""

EXTENSION_URI = "https://a2ui.org/a2a-extension/a2ui/v0.9"
MIME_TYPE = "application/json+a2ui"
BASIC_CATALOG_ID = "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"
