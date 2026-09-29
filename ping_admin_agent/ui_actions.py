"""Validated UI commands. Target arguments are owned by the server."""

import json
import re
import secrets
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime

from google.protobuf.json_format import MessageToDict


class UiActionError(ValueError):
    pass


class UiValidationError(UiActionError):
    def __init__(self, view, values, errors):
        super().__init__("Check the listed fields before continuing.")
        self.view, self.values, self.errors = view, values, errors


@dataclass(frozen=True)
class Field:
    kind: str = "text"
    required: bool = False
    limit: int = 255
    choices: tuple[str, ...] = ()
    # The tool argument this field fills, when it differs from the field's own
    # name. Two pickers on one form can then both supply `membership_id`.
    arg: str | None = None

    def validate(self, value):
        if self.kind == "bool":
            if type(value) is not bool:
                raise ValueError("Choose yes or no.")
            return value
        if self.choices:
            if isinstance(value, list) and len(value) == 1:
                value = value[0]
            if not isinstance(value, str) or value not in self.choices:
                raise ValueError("Choose a listed option.")
            return value
        if not isinstance(value, str):
            raise ValueError("Enter text.")  # noqa: TRY004 -- Unified field-validation error contract.
        value = value.strip()
        if self.required and not value:
            raise ValueError("This field is required.")
        allowed = "\n\r\t" if self.kind == "multiline" else ""
        if len(value) > self.limit or any(
            ord(c) < 32 and c not in allowed for c in value
        ):
            raise ValueError(
                f"Use at most {self.limit} characters without control characters."
            )
        if (
            self.kind == "resource"
            and value
            and (value in {".", ".."} or any(c in value for c in "/\\?#%"))
        ):
            raise ValueError("Enter an exact resource ID, not a URL or path.")
        return value


@dataclass
class UiCommand:
    kind: str
    target: str
    args: dict = field(default_factory=dict)
    # Where the control was shown, such as the account a drill-down came from.
    # Kept on the server with the command and never passed to the tool.
    origin: dict = field(default_factory=dict)


@dataclass
class ActionSpec:
    name: str
    command: UiCommand
    fields: dict[str, Field] = field(default_factory=dict)
    once: bool = False
    action_id: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    consumed: bool = False


def _data_parts(message) -> list:
    return [MessageToDict(part.data) for part in message.parts if part.HasField("data")]


def _envelopes(values: list, key: str) -> dict:
    """Distinct A2UI envelopes of one kind, keyed by their canonical JSON."""
    return {
        json.dumps(value, sort_keys=True): value
        for value in values
        if isinstance(value, dict) and set(value) == {"version", key}
    }


def action_envelope(message) -> dict | None:
    data = _data_parts(message)
    if not data:
        return None
    # A click can arrive beside other parts, such as a text label or a repeat
    # of the same envelope. Exactly one distinct action is accepted.
    actions = _envelopes(data, "action")
    if not actions:
        raise UiActionError(
            "Unsupported structured input. Use the current controls or a text request."
        )
    if len(actions) != 1:
        raise UiActionError("Send a single UI action.")
    value = next(iter(actions.values()))
    if value["version"] != "v0.9":
        raise UiActionError(
            "Unsupported structured input. Use the current controls or a text request."
        )
    event = value["action"]
    keys = {"name", "surfaceId", "sourceComponentId", "timestamp", "context"}
    if not isinstance(event, dict) or set(event) != keys:
        raise UiActionError("Incomplete UI action.")
    if not all(
        isinstance(event[k], str) and 0 < len(event[k]) <= 200
        for k in keys - {"context"}
    ):
        raise UiActionError("Invalid UI action identifier.")
    if not isinstance(event["context"], dict):
        raise UiActionError("Invalid action context.")
    try:
        if (
            datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).tzinfo
            is None
        ):
            raise ValueError
    except ValueError:
        raise UiActionError("Use a timestamp with a timezone.") from None
    return event


def surface_record(surface, context_id: str, user_id: str, task_id: str) -> dict:
    return {
        "context_id": context_id,
        "user_id": user_id,
        "task_id": task_id,
        "view": surface.view,
        "actions": {
            key: {**asdict(value), "view": surface.view}
            for key, value in surface.actions.items()
        },
    }


def merge_surface_record(existing: dict | None, record: dict) -> dict:
    """Keep the earlier controls of a refreshed surface valid beside new ones."""
    if (
        not existing
        or existing["context_id"] != record["context_id"]
        or existing["user_id"] != record["user_id"]
    ):
        return record
    return {**record, "actions": {**existing["actions"], **record["actions"]}}


def resolve_action(
    record: dict | None,
    event: dict,
    context_id: str,
    user_id: str,
    *,
    validate_fields=True,
) -> UiCommand:
    if not record or record["context_id"] != context_id or record["user_id"] != user_id:
        raise UiActionError(
            "This view expired or belongs to another conversation. Open a fresh view."
        )
    spec = record["actions"].get(event["sourceComponentId"])
    token = event["context"].get("actionId")
    if (
        not spec
        or spec["name"] != event["name"]
        or spec["consumed"]
        or not isinstance(token, str)
        or not secrets.compare_digest(spec["action_id"], token)
    ):
        raise UiActionError(
            "This control has expired or was already used. Use the latest view."
        )
    supplied = event["context"]
    if set(supplied) - {"actionId", *spec["fields"]}:
        raise UiActionError(
            "Unexpected action fields. The selected target cannot be changed."
        )
    command = UiCommand(**deepcopy(spec["command"]))
    if not validate_fields:
        return command
    values, errors, targets = {}, {}, {}
    for key, config in spec["fields"].items():
        rule = Field(**config)
        targets[key] = rule.arg or key
        raw = supplied.get(key, False if rule.kind == "bool" else "")
        try:
            values[key] = rule.validate(raw)
        except ValueError as exc:
            errors[key] = str(exc)
            values[key] = raw[: rule.limit] if isinstance(raw, str) else ""
    if errors:
        raise UiValidationError(
            spec.get("view", record["view"]),
            {**deepcopy(spec["command"]["args"]), **values},
            errors,
        )
    command.args.update({targets[key]: value for key, value in values.items()})
    return command


def decision_text(message):
    text = "\n".join(p.text for p in message.parts if p.HasField("text")).strip()
    match = re.fullmatch(r"(approve|reject) ([A-Za-z0-9_-]{20,100})", text)
    return (match[2], match[1] == "approve") if match else None


def renderer_error(message):
    data = _data_parts(message)
    errors = [
        value
        for value in _envelopes(data, "error").values()
        if value["version"] == "v0.9" and isinstance(value["error"], dict)
    ]
    return bool(errors) and not _envelopes(data, "action")
