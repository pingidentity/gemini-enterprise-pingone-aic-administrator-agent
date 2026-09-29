"""Invoke a user search through Agent Runtime's real A2A 0.3 streaming API.

Uses ADC, requests A2UI, and checks structured output without printing user
records. This calls a live agent; the normal pytest suite does not.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from uuid import uuid4

import google.auth
import httpx
from a2a.compat.v0_3.rest_transport import CompatRestTransport
from a2a.types import Message, Part, Role, SendMessageRequest, TaskState
from a2a.utils.errors import A2AError
from google.auth.exceptions import DefaultCredentialsError
from google.auth.transport.requests import Request
from google.protobuf.json_format import MessageToDict

from deployment.deploy import runtime_url
from ping_admin_agent import a2ui
from ping_admin_agent.a2a import CLOUD_PLATFORM_SCOPE
from ping_admin_agent.ui_protocol import (
    BASIC_CATALOG_ID,
    GEMINI_CATALOG_ID,
    validate_surface,
)


def explain_access_error(detail: str) -> str:
    """Translate Google's front-door rejections; the agent never sees them."""
    if "401" in detail:
        return (
            "Agent Runtime returned 401: no valid Google OAuth2 access token reached "
            "it. Google rejects unauthenticated requests and identity tokens before "
            "the agent runs; use an access token with the cloud-platform scope "
            "(`gcloud auth application-default login`). From Gemini Enterprise, "
            "attach the cloud-platform OAuth authorization to the registration "
            "(agentAuthorization) and have the operator complete the consent."
        )
    if "403" in detail:
        return (
            "Agent Runtime returned 403: the token is valid but its identity lacks "
            "aiplatform.reasoningEngines.query (Vertex AI User) on the runtime project."
        )
    return f"Agent Runtime request failed: {detail}"


def ui_messages(event) -> list[dict]:
    """Extract UI only from structured A2A parts, never from text matching JSON."""
    kind = event.WhichOneof("payload")
    if kind == "artifact_update":
        parts = event.artifact_update.artifact.parts
    elif kind == "status_update":
        parts = event.status_update.status.message.parts
    elif kind == "message":
        parts = event.message.parts
    elif kind == "task":
        parts = [part for artifact in event.task.artifacts for part in artifact.parts]
    else:
        parts = []
    return [
        MessageToDict(part.data)
        for part in parts
        if part.WhichOneof("content") == "data"
        and MessageToDict(part.metadata).get("mimeType") == a2ui.MIME_TYPE
    ]


async def check(
    resource_name: str, search_term: str, catalog: str = GEMINI_CATALOG_ID
) -> None:
    url = runtime_url(resource_name)
    try:
        credentials, _ = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
        credentials.refresh(Request())
    except DefaultCredentialsError as exc:
        raise SystemExit(
            "No Application Default Credentials. Agent Runtime accepts only Google "
            "OAuth2 access tokens with the cloud-platform scope; run "
            "`gcloud auth application-default login` and retry."
        ) from exc
    headers = {
        "Authorization": f"Bearer {credentials.token}",
        "X-A2A-Extensions": a2ui.EXTENSION_URI,
    }
    prompt = (
        "Use search_users to find users matching "
        + json.dumps(search_term)
        + ". Perform only this search; do not change any users or configuration."
    )
    request = SendMessageRequest(
        message=Message(
            message_id=uuid4().hex,
            role=Role.ROLE_USER,
            parts=[Part(text=prompt)],
            metadata={
                "a2uiClientCapabilities": {"v0.9": {"supportedCatalogIds": [catalog]}}
            },
        )
    )
    messages = []
    completed = False
    try:
        async with httpx.AsyncClient(headers=headers, timeout=120) as http:
            transport = CompatRestTransport(
                httpx_client=http, agent_card=None, url=url
            )
            async for event in transport.send_message_streaming(request):
                if event.HasField("status_update"):
                    state = event.status_update.status.state
                    if state == TaskState.TASK_STATE_COMPLETED:
                        completed = True
                        messages = ui_messages(event)
                    elif state in {
                        TaskState.TASK_STATE_FAILED,
                        TaskState.TASK_STATE_CANCELED,
                        TaskState.TASK_STATE_REJECTED,
                    }:
                        raise RuntimeError(
                            "Agent returned a failed, canceled, or rejected task"
                        )
    except A2AError as exc:
        raise SystemExit(explain_access_error(str(exc))) from exc
    if not completed:
        raise RuntimeError("No completed A2A task was received")
    if not messages:
        raise RuntimeError(
            "No A2UI messages received; use a term with at least one matching user"
        )
    validate_surface(messages, catalog)
    if not any("createSurface" in message for message in messages):
        raise RuntimeError("The completed response has no UI surface")
    print("PASS: A2A 0.3 streaming completed with a schema-valid A2UI workspace.")
    print("Next, run the same search in Gemini Enterprise and verify visual rendering.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("resource_name")
    parser.add_argument("--search-term", required=True)
    parser.add_argument("--catalog", choices=["gemini", "basic"], default="gemini")
    args = parser.parse_args()
    asyncio.run(
        check(
            args.resource_name,
            args.search_term,
            GEMINI_CATALOG_ID if args.catalog == "gemini" else BASIC_CATALOG_ID,
        )
    )


if __name__ == "__main__":
    main()
