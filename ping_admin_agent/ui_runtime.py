"""A2UI front end for AIC tools with temporary, single-use confirmations."""

import asyncio
import hashlib
import inspect
import json
import logging
import secrets
import time
from copy import deepcopy
from uuid import uuid4

from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import TaskUpdater
from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus
from a2a.utils.errors import InvalidParamsError, UnsupportedOperationError
from google.adk.events import Event, EventActions
from google.adk.tools.tool_confirmation import ToolConfirmation
from google.adk.utils.content_utils import SKIP_THOUGHT_SIGNATURE_VALIDATOR
from google.genai import types
from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Value

from . import a2ui
from .confirmed_tool import APPROVED_CALLS, fingerprint
from .ui_actions import (
    UiActionError,
    UiValidationError,
    action_envelope,
    decision_text,
    merge_surface_record,
    renderer_error,
    resolve_action,
    surface_record,
)
from .ui_protocol import (
    GEMINI_CATALOG_ID,
    negotiate_catalog,
    panel_enabled,
    surface_mode,
)
from .ui_results import (
    RuntimeResult,
    ToolResult,
    account_names,
    describe_change,
    describe_result,
    review_args,
    unlinked,
)
from .ui_store import CONFIRMATION_TTL, SESSION_TTL, build_store, conversation_lock
from .ui_views import (
    FORM_FIELDS,
    READ_TOOLS,
    WRITE_TOOLS,
    loading_surface,
    render_result,
    working_update,
)

LOGGER = logging.getLogger(__name__)
CONFIRM = "adk_request_confirmation"
TERMINAL_STATES = {
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_REJECTED,
}


def _heal_thought_signatures(session) -> None:
    """Stamp the bypass placeholder onto stored signature-less function calls.

    Conversations created before 2026-09-25 hold synthetic parts (UI actions,
    confirmation requests) written without a thought signature. Gemini now
    rejects any request whose function-call parts lack one, so every replayed
    model call in such a conversation fails. Healing at read time lets those
    conversations work again; signatures the model issued are left untouched.
    """
    for event in session.events:
        content = event.content
        if not content or not content.parts:
            continue
        for part in content.parts:
            if part.function_call and not part.thought_signature:
                part.thought_signature = SKIP_THOUGHT_SIGNATURE_VALIDATOR


def user_id(context):
    caller = context.call_context.user if context.call_context else None
    return (
        caller.user_name
        if caller and caller.user_name
        else f"A2A_USER_{context.context_id}"
    )


def _message(context_id, task_id, text, messages=()):
    return Message(
        message_id=uuid4().hex,
        role=Role.ROLE_AGENT,
        context_id=context_id,
        task_id=task_id,
        parts=[
            Part(text=text),
            *[
                Part(
                    data=ParseDict(value, Value()),
                    metadata={"mimeType": a2ui.MIME_TYPE},
                )
                for value in messages
            ],
        ],
        extensions=[a2ui.EXTENSION_URI] if messages else [],
    )


# How many groups a picker lists before it asks for a name filter instead.
GROUP_CHOICES = 200
# A live activity view keeps what it has seen for this long and this many events.
TAIL_TTL = 15 * 60
TAIL_EVENTS = 60
TAIL_EVENT_BYTES = 32_000


def _event_identity(record: dict) -> str:
    """Tail repeats the last entry of the previous response; spot the repeat."""
    payload = record.get("payload")
    if isinstance(payload, dict) and payload.get("_id"):
        return str(payload["_id"])
    text = json.dumps(record, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


# Display names remembered per conversation, so a review can name a picked ID.
NAMES_LIMIT = 500


def _membership_choices(kind: str, user_id: str, term: str) -> dict:
    """What a user holds of one kind, and what could still be added."""
    from . import agent
    from .ping_client import PingError

    client = agent.get_client()
    try:
        current = client.list_user_relationship(user_id, kind)
        listed = (
            client.list_groups(term, page_size=GROUP_CHOICES)
            if kind == "groups"
            else client.list_internal_roles(term, page_size=GROUP_CHOICES)
        )
    except PingError as exc:
        return {"kind": kind, "error": str(exc)[:300]}
    except Exception as exc:  # noqa: BLE001 -- The form must still open with its ID fallback.
        LOGGER.warning("Could not list %s (%s)", kind, type(exc).__name__)
        return {"kind": kind, "error": "The tenant did not return a usable list."}

    def records(payload):
        value = payload.get("result") if isinstance(payload, dict) else None
        return [row for row in value or [] if isinstance(row, dict)]

    held = [row for row in records(current) if row.get("_ref")]
    # Removal names the exact reference IDM holds, whatever collection it is in.
    member = [
        {
            "id": row["_ref"],
            "name": row.get("name") or row.get("_refResourceId") or row["_ref"],
        }
        for row in held
    ]
    joined = {row.get("_refResourceId") for row in held}
    found = [row for row in records(listed) if row.get("_id")]
    return {
        "kind": kind,
        "available": [
            {"id": row["_id"], "name": row.get("name") or row["_id"]}
            for row in found
            if row["_id"] not in joined
        ],
        "member": member,
        # A full page means the tenant has more than one picker can hold.
        "truncated": len(found) >= GROUP_CHOICES,
        "filter": term,
    }


class AdministrationRuntime:
    def __init__(self, runner, store):
        self.runner, self.store = runner, store
        self.app_name = runner.app_name

    async def known_names(self, key: str, observations=()) -> dict:
        """Display names known in this conversation, after learning new accounts."""
        known = await self.store.get("names", key) or {}
        learned = account_names(observations)
        if any(known.get(record) != name for record, name in learned.items()):
            known = dict(list({**known, **learned}.items())[-NAMES_LIMIT:])
            await self.store.put("names", key, known, ttl=SESSION_TTL)
        return known

    async def follow_activity(self, key: str, args: dict) -> tuple[dict, dict]:
        """One step of a live tail: what the tool returned, and what to show.

        The monitoring API hands back a cookie to continue from. It stays here,
        with the events seen so far, so every refresh adds to the list.
        """
        from . import agent

        # The first click names no source and a refresh names the selected one;
        # both must find the same cookie.
        source = str(args.get("source") or "am-authentication")
        tail_key = f"{key}\0{args.get('username', '')}\0{source}"
        state = await self.store.get("tail", tail_key) or {}
        response = await asyncio.to_thread(
            agent.get_user_activity, **{**args, "cookie": state.get("cookie", "")}
        )
        if response.get("status") != "success" and state.get("cookie"):
            # A cookie that has expired: start following again rather than fail.
            state = {}
            response = await asyncio.to_thread(
                agent.get_user_activity, **{**args, "cookie": ""}
            )
        if response.get("status") != "success":
            return response, response
        data = response.get("data")
        data = data if isinstance(data, dict) else {}
        events = list(state.get("events", []))
        seen = {_event_identity(record) for record in events}
        added = 0
        for record in data.get("result") or []:
            if not isinstance(record, dict):
                continue
            if len(json.dumps(record, default=str)) > TAIL_EVENT_BYTES:
                record = {**record, "payload": {"message": "Entry too large to keep."}}
            identity = _event_identity(record)
            if identity not in seen:
                seen.add(identity)
                events.append(record)
                added += 1
        events = events[-TAIL_EVENTS:]
        await self.store.put(
            "tail",
            tail_key,
            {
                "cookie": data.get("pagedResultsCookie") or state.get("cookie", ""),
                "events": events,
            },
            ttl=TAIL_TTL,
        )
        shown = {"status": "success", "data": {"result": events, "new": added}}
        return response, shown

    async def form_result(
        self, context_id, actor, view, values, errors=None, text=""
    ) -> RuntimeResult:
        """A form, together with the choices it offers from the tenant."""
        result = RuntimeResult(text, view=view, form_values=values, errors=errors or {})
        if view == "change_user_membership" and values.get("user_id"):
            kind = values.get("kind")
            kind = kind if kind in ("groups", "authzRoles") else "groups"
            term = str(values.get("name_filter") or "")[:80]
            result.choices = await asyncio.to_thread(
                _membership_choices, kind, values["user_id"], term
            )
            offered = {
                item["id"]: item["name"]
                for key in ("available", "member")
                for item in result.choices.get(key, [])
            }
            if offered:
                key = self.key(context_id, actor)
                known = await self.store.get("names", key) or {}
                merged = {**known, **offered}
                # Keep the newest names when a long session outgrows the limit.
                kept = dict(list(merged.items())[-NAMES_LIMIT:])
                await self.store.put("names", key, kept, ttl=SESSION_TTL)
        return result

    @staticmethod
    def key(context_id, actor):
        return f"{actor}\0{context_id}"

    async def session(self, context_id, actor, *, required=False):
        key = self.key(context_id, actor)
        mapping = await self.store.get("session", key)
        # Agent Runtime assigns its own session IDs. An A2A context ID cannot
        # be used to recover a session after this process's mapping is lost.
        session = (
            await self.runner.session_service.get_session(
                app_name=self.app_name, user_id=actor, session_id=mapping["id"]
            )
            if mapping
            else None
        )
        if session is None:
            if required:
                raise UiActionError(
                    "The original conversation is unavailable. Request the change again."
                )
            session = await self.runner.session_service.create_session(
                app_name=self.app_name, user_id=actor
            )
            await self.store.put("session", key, {"id": session.id}, ttl=SESSION_TTL)
        _heal_thought_signatures(session)
        return session

    async def pending_result(self, context_id, actor):
        pending = await self.store.get("pending", self.key(context_id, actor))
        if not pending or pending["expires_at"] <= time.time():
            return None
        confirmations = [
            {"id": token, "tool": record["tool"]}
            for token, record in pending["calls"].items()
            if token not in pending["decisions"]
        ]
        if not confirmations:
            return None
        lines = [
            (
                "Review each exact change and approve or reject it in the panel or "
                "with the commands below. No pending change has been executed."
            )
        ]
        for item in confirmations:
            lines += [
                describe_change(item["tool"]["name"], item["tool"]["args"]),
                f"approve {item['id']}",
                f"reject {item['id']}",
            ]
        lines.append(
            "Reply with one complete command. Approvals expire after 10 minutes."
        )
        return RuntimeResult("\n".join(lines), confirmations=confirmations)

    async def _save_pending(self, calls, context_id, actor, task_id):
        record = {
            "task_id": task_id,
            "expires_at": time.time() + CONFIRMATION_TTL,
            "decisions": {},
            "calls": {
                secrets.token_urlsafe(24): {
                    "id": call.id,
                    "tool": {
                        "name": call.args["originalFunctionCall"]["name"],
                        "args": review_args(
                            call.args["originalFunctionCall"].get("args", {})
                        ),
                    },
                }
                for call in calls
            },
        }
        await self.store.put(
            "pending", self.key(context_id, actor), record, CONFIRMATION_TTL
        )
        return await self.pending_result(context_id, actor)

    async def _run(
        self, session, content, context_id, actor, task_id, *, grants=None, rejected=()
    ):
        original = {
            call.id: call
            for event in session.events
            for call in event.get_function_calls()
        }
        observations, confirmations, texts = [], [], []
        unavailable = False
        token = APPROVED_CALLS.set(grants or {})
        try:
            async for event in self.runner.run_async(
                user_id=actor, session_id=session.id, new_message=content
            ):
                for part in event.content.parts if event.content else []:
                    call, response = part.function_call, part.function_response
                    if call and call.name == CONFIRM:
                        target = (call.args or {}).get("originalFunctionCall", {})
                        if (
                            not call.id
                            or target.get("name") not in WRITE_TOOLS
                            or len(json.dumps(target)) > 16000
                        ):
                            raise ValueError("Invalid confirmation event")
                        confirmations.append(call)
                    elif call:
                        original[call.id] = call
                    elif (
                        response
                        and response.name in READ_TOOLS | WRITE_TOOLS
                        and not event.actions.requested_tool_confirmations
                    ):
                        response_data = (
                            {"status": "rejected"}
                            if response.id in rejected
                            else deepcopy(response.response or {})
                        )
                        observations.append(
                            ToolResult(
                                response.name,
                                response_data,
                                deepcopy(original[response.id].args or {})
                                if response.id in original
                                else {},
                            )
                        )
                    elif part.text and not part.thought and event.is_final_response():
                        texts.append(part.text)
        except Exception:
            if not observations and not confirmations:
                raise
            LOGGER.warning("Model follow-up failed after a known tool outcome")
            texts = [
                "Model follow-up was unavailable. Review these returned tool outcomes before retrying:"
            ]
            unavailable = True
        finally:
            APPROVED_CALLS.reset(token)
        # A lookup earlier in this same turn already tells us who the account is.
        names = await self.known_names(self.key(context_id, actor), observations)
        described = "\n".join(
            describe_result(item, names=names) for item in observations
        )
        if unavailable:
            texts = [f"{texts[0]}\n{described}"]
        if confirmations:
            result = await self._save_pending(confirmations, context_id, actor, task_id)
            result.observations = observations
            if observations:
                result.text = described + "\n" + result.text
            return result
        # Always deliver observed write outcomes (including generated passwords),
        # even if the model omits them or gives a contradictory follow-up.
        outcome = (
            described
            if any(item.name in WRITE_TOOLS for item in observations)
            else "\n".join(texts)
        )
        return RuntimeResult(
            outcome or described or "The request completed without a result.",
            observations=observations,
        )

    async def _history(self, session, name, args):
        invocation = "ui-" + uuid4().hex
        call = types.FunctionCall(
            id="ui-call-" + uuid4().hex, name=name, args=deepcopy(args)
        )
        # A fabricated part the model never produced: the placeholder
        # signature on the Part lets the Gemini backend accept it (enforced
        # since 2026-09-25; missing signatures fail the whole turn with 400).
        part = types.Part(
            function_call=call, thought_signature=SKIP_THOUGHT_SIGNATURE_VALIDATOR
        )
        for author, content in [
            (
                "user",
                types.Content(role="user", parts=[types.Part(text=f"UI request: {name}")]),
            ),
            (self.runner.agent.name, types.Content(role="model", parts=[part])),
        ]:
            await self.runner.session_service.append_event(
                session=session,
                event=Event(invocation_id=invocation, author=author, content=content),
            )
        return invocation, call

    async def _prepare(self, name, args, context_id, actor, task_id, consume):
        from . import agent

        values = deepcopy(args)
        try:
            if name == "create_oidc_application":
                args["redirect_uris"] = list(
                    dict.fromkeys(
                        v.strip()
                        for v in args["redirect_uris"].splitlines()
                        if v.strip()
                    )
                )
                args.update(
                    scopes=["openid"],
                    grant_types=["authorization_code"],
                    response_types=["code"],
                    token_endpoint_auth_method="client_secret_basic"
                    if args["app_type"] == "web"
                    else "none",
                )
            elif name == "create_saml_application":
                for field in ("sso_entities", "app_type_specific_config"):
                    args[field] = json.loads(args[field])
                args.update(
                    icon="",
                    template_name=agent.SAML_TEMPLATE_NAME,
                    template_version=agent.SAML_TEMPLATE_VERSION,
                )
            inspect.signature(getattr(agent, name)).bind(**args)
            # Existing functions validate before their confirm=False gate; this
            # invokes no tenant API, even when validation fails.
            validation = getattr(agent, name)(**args, confirm=False)
            if validation.get(
                "status"
            ) == "error" and "without an explicit confirmation" not in validation.get(
                "message", ""
            ):
                raise ValueError(validation.get("message", "Invalid change"))
        except (ValueError, TypeError, KeyError) as exc:
            raise UiValidationError(name, values, {"change": str(exc)}) from None
        # Validation is reversible; staging is single-use even if the worker
        # exits between writing ADK history and saving the review.
        await consume()
        session = await self.session(context_id, actor)
        invocation, original = await self._history(session, name, args)
        confirmation = ToolConfirmation(confirmed=False)
        await self.runner.session_service.append_event(
            session=session,
            event=Event(
                invocation_id=invocation,
                author=self.runner.agent.name,
                content=types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            function_response=types.FunctionResponse(
                                id=original.id,
                                name=name,
                                response={"error": "This tool requires confirmation."},
                            )
                        )
                    ],
                ),
                actions=EventActions(
                    requested_tool_confirmations={original.id: confirmation}
                ),
            ),
        )
        call = types.FunctionCall(
            id="ui-confirm-" + uuid4().hex,
            name=CONFIRM,
            args={
                "originalFunctionCall": original.model_dump(exclude_none=True),
                "toolConfirmation": {"confirmed": False},
            },
        )
        # Same placeholder as the UI-action part above: the confirmation call
        # is synthesized here, so the Part carries the bypass signature.
        part = types.Part(
            function_call=call, thought_signature=SKIP_THOUGHT_SIGNATURE_VALIDATOR
        )
        await self.runner.session_service.append_event(
            session=session,
            event=Event(
                invocation_id=invocation,
                author=self.runner.agent.name,
                content=types.Content(role="model", parts=[part]),
                long_running_tool_ids={call.id},
            ),
        )
        return await self._save_pending([call], context_id, actor, task_id)

    async def _decide(self, decision, context_id, actor, task_id):
        key, (token, confirmed) = self.key(context_id, actor), decision

        def decide(pending):
            if (
                not pending
                or pending["expires_at"] <= time.time()
                or token not in pending["calls"]
                or token in pending["decisions"]
            ):
                raise UiActionError(
                    "This approval is unknown, expired, or already used."
                )
            pending["decisions"][token] = confirmed
            return pending

        pending = await self.store.mutate("pending", key, decide, CONFIRMATION_TTL)
        if len(pending["decisions"]) != len(pending["calls"]):
            return await self.pending_result(context_id, actor)
        # Consume before retrieving history as well as before execution. Missing
        # history must not leave a resolved review blocking a fresh request.
        await self.store.put("pending", key, None)
        session = await self.session(context_id, actor, required=True)
        calls = {
            call.id: call
            for event in session.events
            for call in event.get_function_calls()
        }
        parts, grants, rejected = [], {}, set()
        for approval, record in pending["calls"].items():
            call = calls.get(record["id"])
            if not call or call.name != CONFIRM:
                raise UiActionError(
                    "The original confirmation is unavailable. Request the change again."
                )
            original = call.args["originalFunctionCall"]
            if pending["decisions"][approval]:
                grants[original["id"]] = fingerprint(
                    original["name"], original.get("args", {})
                )
            else:
                rejected.add(original["id"])
            parts.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        id=call.id,
                        name=CONFIRM,
                        response={"confirmed": pending["decisions"][approval]},
                    )
                )
            )
        # Consume before execution. A process exit discards all confirmations;
        # the front end never automatically retries a change after a crash.
        return await self._run(
            session,
            types.Content(role="user", parts=parts),
            context_id,
            actor,
            task_id,
            grants=grants,
            rejected=rejected,
        )

    async def query(self, context, catalog):
        context_id, actor, task_id = (
            context.context_id,
            user_id(context),
            context.task_id,
        )
        key = self.key(context_id, actor)
        async with conversation_lock(self.store, key):
            decision = decision_text(context.message)
            event = action_envelope(context.message)
            pending = await self.pending_result(context_id, actor)
            if decision:
                return await self._decide(decision, context_id, actor, task_id)
            if event:
                if not catalog:
                    raise UiActionError(
                        "This control needs a supported A2UI 0.9 catalog."
                    )
                record = await self.store.get("surface", event["surfaceId"])
                command = resolve_action(record, event, context_id, actor)
                if pending and command.kind != "confirm":
                    return pending
                if command.kind == "confirm":
                    return await self._decide(
                        (command.args["id"], command.args["confirmed"]),
                        context_id,
                        actor,
                        task_id,
                    )
                if command.kind == "view":
                    if command.target not in {"workspace", *FORM_FIELDS}:
                        raise UiActionError("Unknown view")
                    return await self.form_result(
                        context_id, actor, command.target, command.args
                    )
                if command.kind == "prepare" and command.target in WRITE_TOOLS:

                    def consume(value):
                        resolve_action(value, event, context_id, actor)
                        value["actions"][event["sourceComponentId"]]["consumed"] = True
                        return value

                    # Validate form before consuming; a corrected submission can retry.
                    return await self._prepare(
                        command.target,
                        command.args,
                        context_id,
                        actor,
                        task_id,
                        lambda: self.store.mutate(
                            "surface", event["surfaceId"], consume
                        ),
                    )
                if command.kind == "read" and command.target in READ_TOOLS:
                    from . import agent

                    if command.target == "find_application":
                        command.args["query"] = command.args.pop("application_query")
                    if command.target == "get_user_activity" and command.args.get(
                        "live"
                    ):
                        response, shown = await self.follow_activity(key, command.args)
                    else:
                        response = shown = await asyncio.to_thread(
                            getattr(agent, command.target), **command.args
                        )
                    session = await self.session(context_id, actor)
                    invocation, call = await self._history(
                        session, command.target, command.args
                    )
                    await self.runner.session_service.append_event(
                        session=session,
                        event=Event(
                            invocation_id=invocation,
                            author=self.runner.agent.name,
                            content=types.Content(
                                role="user",
                                parts=[
                                    types.Part(
                                        function_response=types.FunctionResponse(
                                            id=call.id,
                                            name=call.name,
                                            response=response,
                                        )
                                    )
                                ],
                            ),
                        ),
                    )
                    # The conversation history gets what the tool returned; the
                    # panel gets everything a live view has gathered so far.
                    item = ToolResult(
                        command.target, shown, command.args, command.origin
                    )
                    names = await self.known_names(key, [item])
                    return RuntimeResult(
                        describe_result(item, brief=bool(catalog), names=names),
                        observations=[item],
                    )
                raise UiActionError("Unknown action")
            if pending:
                return pending
            text = "\n".join(
                p.text for p in context.message.parts if p.HasField("text")
            ).strip()
            if (
                not text
                or len(text) > 16000
                or len(context.message.parts)
                != sum(p.HasField("text") for p in context.message.parts)
            ):
                raise UiActionError("Send a text request of at most 16000 characters.")
            if text.lower().startswith(("approve ", "reject ")):
                raise UiActionError(
                    "Use the complete approval command from the current review."
                )
            session = await self.session(context_id, actor)
            return await self._run(
                session,
                types.Content(role="user", parts=[types.Part(text=text)]),
                context_id,
                actor,
                task_id,
            )


class AdministrationExecutor(AgentExecutor):
    def __init__(self, runner_builder, store=None):
        self.runner_builder, self.store = runner_builder, store
        self.runtime = None

    def _runtime(self):
        if self.runtime is None:
            self.store = self.store or build_store()
            runner = self.runner_builder()
            self.runtime = AdministrationRuntime(runner, self.store)
        return self.runtime

    async def execute(self, context, event_queue):
        if not context.message or context.message.role != Role.ROLE_USER:
            raise InvalidParamsError("A user message is required")
        task = context.current_task or Task(
            id=context.task_id,
            context_id=context.context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
        )
        if context.current_task is None:
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        catalog = negotiate_catalog(context)
        render_error = renderer_error(context.message)
        if render_error:
            catalog = None
        loading_id = "loading-" + uuid4().hex
        state = TaskState.TASK_STATE_COMPLETED
        # One side-panel surface per conversation: created on the first
        # response and refreshed in place afterwards, so the panel updates
        # instead of opening a new tab for every click.
        persistent = (
            catalog == GEMINI_CATALOG_ID
            and panel_enabled()
            and surface_mode() == "conversation"
        )
        key = canvas = None
        try:
            runtime = self._runtime()
            key = runtime.key(task.context_id, user_id(context))
            canvas = await self.store.get("canvas", key) if persistent else None
            if catalog and canvas:
                # Progress shows in the existing panel's header, where it moves
                # nothing, instead of a transient surface flashing in the chat.
                await updater.update_status(
                    TaskState.TASK_STATE_WORKING,
                    _message(
                        task.context_id,
                        task.id,
                        "Working on your request",
                        working_update(canvas, catalog),
                    ),
                )
            elif catalog:
                await updater.update_status(
                    TaskState.TASK_STATE_WORKING,
                    _message(
                        task.context_id,
                        task.id,
                        "Working on your request",
                        loading_surface(loading_id, catalog).messages(),
                    ),
                )
            else:
                await updater.update_status(TaskState.TASK_STATE_WORKING)
            if render_error:
                raise UiActionError(
                    "The client reported a rendering problem. Continue with text or an approval command."
                )
            result = await runtime.query(context, catalog)
        except UiValidationError as exc:
            try:
                # The corrected form needs its pickers again, not just its text.
                result = await runtime.form_result(
                    task.context_id,
                    user_id(context),
                    exc.view,
                    exc.values,
                    exc.errors,
                    str(exc),
                )
            except Exception:  # noqa: BLE001 -- A form without choices still explains the error.
                result = RuntimeResult(
                    str(exc), view=exc.view, form_values=exc.values, errors=exc.errors
                )
        except UiActionError as exc:
            result = RuntimeResult(str(exc))
            state = TaskState.TASK_STATE_FAILED
            if self.runtime:
                pending = await self.store.get(
                    "pending", self.runtime.key(task.context_id, user_id(context))
                )
                if pending:
                    review = await self.runtime.pending_result(
                        task.context_id, user_id(context)
                    )
                    if review:
                        result = review
                        result.text = str(exc) + "\n" + result.text
        except Exception as exc:  # noqa: BLE001 -- Never log provider exception payloads or credentials.
            LOGGER.error("Agent request failed (%s)", type(exc).__name__)
            result = RuntimeResult(
                "The request could not be completed. If you approved a change, check its state in AIC before retrying."
            )
            state = TaskState.TASK_STATE_FAILED
        messages = []
        if catalog and self.runtime and key:
            try:
                # Lets a review and its receipt name what a picker selected.
                result.names = await self.store.get("names", key) or {}
            except Exception:  # noqa: BLE001 -- Names are a nicety; IDs still identify the change.
                result.names = {}
        if catalog and self.runtime:
            if not canvas:
                messages.append(
                    {"version": "v0.9", "deleteSurface": {"surfaceId": loading_id}}
                )
            try:
                surface = render_result(
                    result,
                    canvas["id"] if canvas else "pingaic-" + uuid4().hex,
                    catalog,
                )
                rendered = surface.messages(create=canvas is None)
                record = surface_record(
                    surface, task.context_id, user_id(context), task.id
                )
                await self.store.mutate(
                    "surface",
                    surface.surface_id,
                    lambda existing: merge_surface_record(existing, record),
                )
                if persistent and surface.canvas:
                    # Remember the panel's header so the next turn can show
                    # progress in it before the full refresh.
                    await self.store.put(
                        "canvas",
                        key,
                        {"id": surface.surface_id, **surface.canvas},
                        ttl=SESSION_TTL,
                    )
                # Retire the previous review on the client. Its server approval
                # tokens are already consumed, irrespective of renderer behavior.
                previous = await self.store.get("review", key)
                if previous and previous["id"] != surface.surface_id:
                    messages.append(
                        {
                            "version": "v0.9",
                            "deleteSurface": {"surfaceId": previous["id"]},
                        }
                    )
                await self.store.put(
                    "review",
                    key,
                    {"id": surface.surface_id} if result.confirmations else None,
                )
                messages.extend(rendered)
            except Exception:  # noqa: BLE001 -- Rendering must preserve completed tool outcomes.
                LOGGER.warning("Could not construct or retain the UI surface")
                result.text += "\nThe interactive view is unavailable. Use the text result and complete approval commands."
                if canvas:
                    # Put the original header back; the previous view stays.
                    messages.extend(working_update(canvas, catalog, working=False))
        if result.confirmations and not catalog:
            # Text clients get an input-required task and answer with the exact
            # approve/reject command. A2UI clients get Approve/Reject controls
            # in the surface of a completed task instead: Gemini Enterprise
            # answers input-required with its own generic text prompt.
            state = TaskState.TASK_STATE_INPUT_REQUIRED
        await updater.update_status(
            state,
            _message(
                task.context_id,
                task.id,
                # Covers tool receipts and the model's own wording alike.
                unlinked(result.text or "The view is ready."),
                messages,
            ),
        )

    async def cancel(self, context, event_queue):
        raise UnsupportedOperationError("Cancellation is unavailable")


class UiRequestHandler(DefaultRequestHandler):
    """Bind UI controls and approval commands to their task before the SDK runs.

    Only the SDK's public send operations are overridden. It creates or resumes
    the A2A task from ``params.message`` afterwards, so adjusting the message's
    task ID here routes a control to the review it belongs to.
    """

    async def on_message_send(self, params, context):
        await self._bind_task(params, context)
        return await super().on_message_send(params, context)

    async def on_message_send_stream(self, params, context):
        await self._bind_task(params, context)
        async for event in super().on_message_send_stream(params, context):
            yield event

    async def _bind_task(self, params, call_context):
        try:
            await self._bind_ui_task(params, call_context)
        except UiActionError as exc:
            raise InvalidParamsError(str(exc)) from None

    async def _bind_ui_task(self, params, call_context):
        if renderer_error(params.message):
            # The client could not render a surface; answer in a fresh task.
            params.message.ClearField("task_id")
            return
        # Unsupported structured input is rejected here, before a task exists.
        event = action_envelope(params.message)
        if not params.message.context_id:
            return
        actor = (
            call_context.user.user_name
            if call_context and call_context.user and call_context.user.user_name
            else f"A2A_USER_{params.message.context_id}"
        )
        if event:
            store = self.agent_executor._runtime().store
            record = await store.get("surface", event["surfaceId"])
            resolve_action(
                record, event, params.message.context_id, actor, validate_fields=False
            )
            # Every control opens a new task. Approvals are bound to the
            # conversation, the actor and a single-use token, never to the task
            # that rendered them, so the side panel outlives any one task and a
            # completed review task is never addressed again.
            params.message.ClearField("task_id")
            return
        if params.message.task_id:
            return
        runtime = self.agent_executor._runtime()
        pending = await runtime.store.get(
            "pending", runtime.key(params.message.context_id, actor)
        )
        if (
            not pending
            or pending["expires_at"] <= time.time()
            or len(pending["decisions"]) >= len(pending["calls"])
        ):
            return
        # A text client that omits the task ID continues its input-required
        # review task. A review issued to an A2UI client is already complete
        # and cannot be continued; the message opens a new task instead.
        task = await self.task_store.get(pending["task_id"], call_context)
        if task and task.status.state not in TERMINAL_STATES:
            params.message.task_id = pending["task_id"]
