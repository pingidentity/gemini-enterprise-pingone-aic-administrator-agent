"""Small, deterministic component vocabulary shared by Material and basic views."""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .branding import logo_url
from .ui_actions import ActionSpec, Field, UiCommand
from .ui_protocol import GEMINI_CATALOG_ID, WIRE_VERSION, validate_surface
from .ui_results import unlinked

# The Basic catalog knows fewer text styles than the Material catalog.
BASIC_TEXT_VARIANTS = {
    "subtitle1": "h5",
    "subtitle2": "h5",
    "body1": "body",
    "body2": "body",
    "overline": "caption",
}
# The Basic catalog only allows a fixed set of icon names.
BASIC_ICONS = {
    "admin_panel_settings": "settings",
    "check_circle": "check",
    "block": "close",
    "info": "info",
    "warning": "warning",
    "error": "error",
    "search": "search",
    "refresh": "refresh",
    "person": "person",
    "group": "person",
    "badge": "accountCircle",
    "assignment": "folder",
    "close": "close",
}
BANNER_ICONS = {
    "success": ("check_circle", "primary"),
    "info": ("info", "primary"),
    "warning": ("warning", "warn"),
    "error": ("error", "warn"),
}


def display(value: Any, limit: int = 512) -> str:
    if value is None:
        return "Not available"
    if type(value) is bool:
        return "Yes" if value else "No"
    if isinstance(value, float) and not math.isfinite(value):
        return "Not available"
    if isinstance(value, (str, int, float)):
        return str(value)[:limit]
    return "Not available"


@dataclass
class Surface:
    surface_id: str
    catalog_id: str
    view: str
    components: list[dict[str, Any]]
    data: dict[str, Any]
    actions: dict[str, ActionSpec]
    canvas: dict[str, Any] | None = None

    def messages(self, *, create: bool = True) -> list[dict[str, Any]]:
        messages = []
        if create:
            messages.append(
                {
                    "version": WIRE_VERSION,
                    "createSurface": {
                        "surfaceId": self.surface_id,
                        "catalogId": self.catalog_id,
                        "sendDataModel": False,
                    },
                }
            )
        messages.extend(
            [
                {
                    "version": WIRE_VERSION,
                    "updateDataModel": {
                        "surfaceId": self.surface_id,
                        "value": self.data,
                    },
                },
                {
                    "version": WIRE_VERSION,
                    "updateComponents": {
                        "surfaceId": self.surface_id,
                        "components": self.components,
                    },
                },
            ]
        )
        # The validator checks each message; lifecycle/reference checks are tested too.
        validate_surface(messages, self.catalog_id)
        return messages


class Components:
    def __init__(
        self, surface_id: str, catalog_id: str, *, values: dict[str, Any] | None = None
    ) -> None:
        self.surface_id, self.catalog_id = surface_id, catalog_id
        self.material = catalog_id == GEMINI_CATALOG_ID
        self.items: list[dict[str, Any]] = []
        self.data: dict[str, Any] = {
            "display": {},
            "form": dict(values or {}),
            "tables": {},
            "charts": {},
        }
        self.actions: dict[str, ActionSpec] = {}
        self.scope = ""
        # Component IDs are unique per render, so a refreshed surface never
        # confuses a control from an earlier render with a new one.
        self.render = uuid4().hex[:6]
        # The header row and its context line, remembered for progress updates.
        self.header: dict[str, Any] | None = None
        # Display names of records by ID, for naming what a change targets.
        self.names: dict[str, str] = {}

    def _field(self, name: str) -> tuple[str, dict[str, str]]:
        key = f"{self.scope}_{name}" if self.scope else name
        return key, {"path": f"/form/{key}"}

    def add(self, component: str, **props: Any) -> str:
        component_id = f"{self.render}c{len(self.items)}"
        self.items.append({"id": component_id, "component": component, **props})
        return component_id

    def component(self, component_id: str) -> dict[str, Any]:
        """A copy of one component's definition, as it will be sent."""
        return deepcopy(next(c for c in self.items if c["id"] == component_id))

    # -- text and media ----------------------------------------------------

    def code(self, text: str, language: str = "json") -> str:
        """Preformatted text, such as the JSON behind an event.

        A fenced block keeps Markdown from reading underscores and asterisks as
        formatting; the style keeps line breaks where text is shown as it is.
        Addresses are left alone here: nothing inside a code block is linked.
        """
        fence = "`" * 3
        key = f"t{len(self.data['display'])}"
        body = display(text, 16000).replace(fence, "` ` `")
        self.data["display"][key] = f"{fence}{language}\n{body}\n{fence}"
        props: dict[str, Any] = {"text": {"path": f"/display/{key}"}}
        if self.material:
            props["usageHint"] = "body2"
            props["style"] = {
                "whiteSpace": "pre-wrap",
                "fontFamily": "monospace",
                "overflowX": "auto",
            }
        else:
            props["variant"] = "body"
        return self.add("MaterialText" if self.material else "Text", **props)

    def hover_label(self, text: Any, tooltip: Any) -> str:
        """Text that shows ``tooltip`` on hover.

        The catalog has tooltips only on buttons, icons, checkboxes and toggles,
        not on text or table cells, so this is a text-style button without an
        action. The Basic catalog has no tooltip at all and gets plain text.
        """
        if self.material:
            return self.add(
                "MaterialButton",
                label=display(text, 200),
                tooltip=display(tooltip, 200),
                appearance="text",
                disableRipple=True,
            )
        return self.text(text)

    def text(
        self,
        text: Any,
        variant: str = "body",
        *,
        weight: float | None = None,
        style: dict[str, str] | None = None,
    ) -> str:
        key = f"t{len(self.data['display'])}"
        # Text components render Markdown; an address in a code span is not linked.
        self.data["display"][key] = unlinked(display(text, 16000))
        props: dict[str, Any] = {"text": {"path": f"/display/{key}"}}
        if self.material:
            props["usageHint"] = variant
        else:
            props["variant"] = BASIC_TEXT_VARIANTS.get(variant, variant)
        if weight is not None and self.material:
            props["weight"] = weight
        if style and self.material:
            props["style"] = style
        return self.add("MaterialText" if self.material else "Text", **props)

    def logo(self, *, compact: bool = False) -> str:
        # The basic Image in both catalogs. Gemini Enterprise's MaterialImage
        # showed a broken image for external URLs (this agent's tests on
        # 2026-09-17 and another team's report), while the composite catalog's
        # basic Image is reported to load them. It has no width or height, so
        # the size comes from the variant hint.
        return self.add(
            "Image",
            url=logo_url(),
            description="Ping Identity",
            fit="contain",
            variant="smallFeature" if compact else "mediumFeature",
        )

    def icon(
        self, name: str, *, color: str | None = None, tooltip: str | None = None
    ) -> str:
        if self.material:
            props: dict[str, Any] = {"icon": name}
            if color:
                props["color"] = color
            if tooltip:
                props["tooltip"] = tooltip
            return self.add("MaterialIcon", **props)
        return self.add("Icon", name=BASIC_ICONS.get(name, "info"))

    def divider(self) -> str:
        return self.add("MaterialDivider" if self.material else "Divider")

    def progress(self, label: str = "Working on your request") -> str:
        if self.material:
            return self.add(
                "MaterialProgressBar", mode="indeterminate", ariaLabel=label
            )
        return self.text(label, "caption")

    # -- layout --------------------------------------------------------------

    def column(self, children: list[str], *, weight: float | None = None) -> str:
        props: dict[str, Any] = {"children": children, "align": "stretch"}
        if self.material:
            props["style"] = {"gap": "12px", "minWidth": "0"}
            if weight is not None:
                props["weight"] = weight
        return self.add("MaterialColumn" if self.material else "Column", **props)

    def row(self, children: list[str], *, align: str = "stretch") -> str:
        props: dict[str, Any] = {"children": children, "align": align}
        if self.material:
            props["style"] = {"gap": "12px", "flexWrap": "wrap"}
        return self.add("MaterialRow" if self.material else "Row", **props)

    def card(self, children: list[str], label: str) -> str:
        if self.material:
            return self.add(
                "MaterialCard",
                children=children,
                appearance="outlined",
                align="stretch",
                ariaLabel=label,
                style={
                    "padding": "16px",
                    "gap": "8px",
                    "flex": "1 1 180px",
                    "minWidth": "0",
                },
            )
        return self.add("Card", child=self.column(children))

    def expansion(
        self,
        title: str,
        description: str,
        children: list[str],
        *,
        expanded: bool = False,
    ) -> str:
        if self.material:
            props: dict[str, Any] = {
                "title": title,
                "expanded": expanded,
                "children": children,
                "ariaLabel": title,
            }
            if description:
                props["description"] = description
            return self.add("MaterialExpansionPanel", **props)
        heading = [self.text(title, "h4")]
        if description:
            heading.append(self.text(description, "caption"))
        return self.card([*heading, *children], title)

    def key_values(self, pairs: list[tuple[str, Any]], *, limit: int = 512) -> str:
        if self.material:
            return self.column(
                [
                    self.row(
                        [
                            self.text(label, "caption", weight=1),
                            self.text(display(value, limit), "body2", weight=2),
                        ],
                        align="center",
                    )
                    for label, value in pairs
                ]
            )
        return self.column(
            [self.text(f"{label}: {display(value, limit)}") for label, value in pairs]
        )

    def status(self, label: str, *, ok: bool) -> str:
        if self.material:
            return self.row(
                [
                    self.icon(
                        "check_circle" if ok else "block",
                        color="primary" if ok else "warn",
                    ),
                    self.text(label, "body2"),
                ],
                align="center",
            )
        return self.text(f"Status: {label}", "caption")

    def banner(self, message: str, kind: str = "info") -> str:
        icon, color = BANNER_ICONS.get(kind, BANNER_ICONS["info"])
        if self.material:
            return self.card(
                [
                    self.row(
                        [
                            self.icon(icon, color=color),
                            self.text(message, "body1", weight=1),
                        ],
                        align="center",
                    )
                ],
                kind.capitalize(),
            )
        return self.card(
            [self.text(f"{kind.capitalize()}: {message}")], kind.capitalize()
        )

    def metrics(self, values: list[tuple[str, Any]]) -> str:
        return self.row(
            [
                self.card([self.text(label, "caption"), self.text(value, "h3")], label)
                for label, value in values
            ]
        )

    def tabs(self, tabs: list[tuple[str, str]]) -> str:
        if self.material:
            return self.add(
                "MaterialTabs",
                tabs=[{"label": title, "content": child} for title, child in tabs],
                ariaLabel="Result details",
            )
        return self.add(
            "Tabs", tabs=[{"title": title, "child": child} for title, child in tabs]
        )

    def table(
        self,
        title: str,
        columns: list[tuple[str, str]],
        rows: list[dict[str, Any]],
        limit: int = 30,
        *,
        cell_limit: int = 512,
    ) -> str:
        if not rows:
            return self.card(
                [self.text(title, "h4"), self.text("No records in this result.")], title
            )
        safe_rows = [
            {key: display(row.get(key), cell_limit) for key, _ in columns}
            for row in rows[:limit]
            if isinstance(row, dict)
        ]
        if self.material:
            key = f"table{len(self.data['tables'])}"
            self.data["tables"][key] = safe_rows
            table = self.add(
                "MaterialTable",
                columns=[{"field": key, "header": label} for key, label in columns],
                rows={"path": f"/tables/{key}"},
                ariaLabel=title,
                caption=title,
                style={"overflowX": "auto", "maxWidth": "100%"},
            )
        else:
            cards = [
                self.card(
                    [self.text(f"{label}: {row[key]}") for key, label in columns], title
                )
                for row in safe_rows
            ]
            table = self.add("List", children=cards, direction="vertical")
        children = [table] if self.material else [self.text(title, "h4"), table]
        if len(rows) > limit:
            children.append(
                self.text(
                    f"Showing {limit} of {len(rows)} returned records.", "caption"
                )
            )
        return self.column(children)

    def distribution(self, title: str, counts: dict[str, Any]) -> str:
        rows = [
            {"label": str(label)[:100], "count": value}
            for label, value in list(counts.items())[:12]
            if type(value) in (int, float) and math.isfinite(value) and value >= 0
        ]
        if not rows:
            return self.text(f"{title}: no recorded values.")
        table = self.table(title, [("label", "Category"), ("count", "Records")], rows)
        if not self.material:
            return table
        key = f"chart{len(self.data['charts'])}"
        # Only embedded values: no URLs, expressions, HTML, or client-generated specs.
        self.data["charts"][key] = {
            "description": title,
            "width": "container",
            "height": max(100, len(rows) * 32),
            "data": {"values": rows},
            "mark": {"type": "bar", "color": "#2563eb"},
            "encoding": {
                "y": {"field": "label", "type": "nominal", "title": None, "sort": "-x"},
                "x": {
                    "field": "count",
                    "type": "quantitative",
                    "title": "Records",
                    "axis": {"tickMinStep": 1},
                },
                "tooltip": [
                    {"field": "label", "type": "nominal", "title": "Category"},
                    {"field": "count", "type": "quantitative", "title": "Records"},
                ],
            },
        }
        chart = self.add(
            "VegaChart",
            spec={"path": f"/charts/{key}"},
            height=max(120, len(rows) * 32),
        )
        return self.column([self.text(title, "h4"), chart, table])

    # -- inputs ---------------------------------------------------------------

    def input(
        self,
        name: str,
        label: str,
        default: str = "",
        *,
        long: bool = False,
        email: bool = False,
        weight: float | None = None,
        placeholder: str = "",
    ) -> str:
        name, binding = self._field(name)
        self.data["form"].setdefault(name, default)
        if self.material and not long:
            props: dict[str, Any] = {
                "label": label,
                "value": binding,
                "type": "email" if email else "text",
                "ariaLabel": label,
            }
            if placeholder:
                props["placeholder"] = placeholder
            if weight is not None:
                props["weight"] = weight
            return self.add("MaterialInput", **props)
        # The basic TextField has no placeholder, so its label carries the hint.
        if placeholder:
            label = f"{label}: {placeholder[0].lower()}{placeholder[1:]}"
        # The composite catalog also supports multiline basic TextField.
        return self.add(
            "TextField",
            label=label,
            value=binding,
            variant="longText" if long else "shortText",
        )

    def choice(
        self,
        name: str,
        label: str,
        options: list[tuple[str, str]],
        default: str,
        *,
        weight: float | None = None,
    ) -> str:
        name, binding = self._field(name)
        allowed = {option[0] for option in options}
        if default not in allowed:
            default = options[0][0]
        value = self.data["form"].setdefault(name, default)
        chosen = value[0] if isinstance(value, list) and value else value
        if chosen not in allowed:
            # A retained selection that is no longer offered, such as a group
            # filtered out of the list, falls back to the default.
            value = self.data["form"][name] = default
        if self.material:
            if isinstance(value, list):
                self.data["form"][name] = value[0] if value else default
            props: dict[str, Any] = {
                "label": label,
                "ariaLabel": label,
                "value": binding,
                "options": [{"value": v, "label": text} for v, text in options],
            }
            if weight is not None:
                props["weight"] = weight
            return self.add("MaterialSelect", **props)
        if isinstance(value, str):
            self.data["form"][name] = [value]
        return self.add(
            "ChoicePicker",
            label=label,
            value=binding,
            variant="mutuallyExclusive",
            displayStyle="chips",
            options=[{"value": value, "label": label} for value, label in options],
        )

    def checkbox(self, name: str, label: str, default: bool = False) -> str:
        name, binding = self._field(name)
        self.data["form"].setdefault(name, default)
        props = {"label": label, "checked" if self.material else "value": binding}
        return self.add("MaterialCheckbox" if self.material else "CheckBox", **props)

    def button(
        self,
        label: str,
        command: UiCommand,
        *,
        fields: dict[str, Field] | None = None,
        primary: bool = False,
        once: bool = False,
        danger: bool = False,
        icon: str | None = None,
        appearance: str | None = None,
    ) -> str:
        fields = fields or {}
        spec = ActionSpec(
            f"pingaic.{command.kind}.{command.target}", command, fields, once
        )
        context = {
            "actionId": spec.action_id,
            **{key: self._field(key)[1] for key in fields},
        }
        action = {"event": {"name": spec.name, "context": context}}
        if self.material:
            props: dict[str, Any] = {
                "label": label,
                "action": action,
                "appearance": appearance or ("filled" if primary else "outlined"),
                "color": "warn" if danger else "primary",
                "ariaLabel": label,
            }
            if icon:
                props["leadingIcon"] = icon
            component = self.add("MaterialButton", **props)
        else:
            component = self.add(
                "Button",
                child=self.text(label),
                action=action,
                variant="primary" if primary else "default",
            )
        self.actions[component] = spec
        return component

    def icon_button(
        self,
        icon: str,
        command: UiCommand,
        *,
        label: str,
        once: bool = False,
        danger: bool = False,
    ) -> str:
        """A compact button that is only an icon, such as an X beside a row.

        ``label`` is what a screen reader announces and what hovering shows.
        """
        spec = ActionSpec(f"pingaic.{command.kind}.{command.target}", command, {}, once)
        action = {"event": {"name": spec.name, "context": {"actionId": spec.action_id}}}
        if self.material:
            component = self.add(
                "MaterialIconButton",
                icon=icon,
                ariaLabel=display(label, 200),
                tooltip=display(label, 200),
                color="warn" if danger else "primary",
                action=action,
            )
        else:
            component = self.add(
                "Button",
                child=self.add("Icon", name=BASIC_ICONS.get(icon, "info")),
                action=action,
                variant="default",
            )
        self.actions[component] = spec
        return component

    def finish(
        self, children: list[str], view: str, *, panel: dict[str, str] | None = None
    ) -> Surface:
        content = self.column(children)
        canvas = None
        if self.material and panel:
            # Canvas must be the surface root. The chat stream then shows a
            # compact opener card and the workspace renders in the persistent,
            # resizable side panel beside the conversation.
            card = {
                "cardTitle": panel["title"],
                "cardDescription": panel["description"],
                "cardIcon": panel["icon"],
                "autoOpen": True,
            }
            self.add("Canvas", children=[content], **card)
            # What a later turn needs to show progress in this panel's header.
            canvas = {"header": self.header}
        self.items[-1]["id"] = "root"
        return Surface(
            self.surface_id,
            self.catalog_id,
            view,
            self.items,
            self.data,
            self.actions,
            canvas,
        )
