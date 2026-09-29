"""Offline integration through the real ADK executor and A2A wire adapters."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import cloudpickle
import httpx
import pytest
import vertexai
from a2a.compat.v0_3.rest_transport import CompatRestTransport
from a2a.types import Message, Part, Role, SendMessageRequest, TaskState
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService, VertexAiSessionService
from google.auth.credentials import AnonymousCredentials
from google.genai import types
from starlette.applications import Starlette

from ping_admin_agent import a2a, a2ui, agent, branding
from ping_admin_agent.runtime import PingAdminA2aAgent
from ping_admin_agent.ui_actions import UiActionError
from ping_admin_agent.ui_protocol import BASIC_CATALOG_ID, validate_surface
from ping_admin_agent.ui_runtime import AdministrationRuntime
from ping_admin_agent.ui_store import MemoryStore
from scripts.smoke_runtime import ui_messages


class SearchModel(BaseLlm):
    model: str = "offline-test"
    calls: int = 0

    async def generate_content_async(self, llm_request, stream=False):
        # UI output from earlier searches must never go back into Vertex AI.
        assert all(
            not part.part_metadata
            for content in llm_request.contents
            for part in content.parts or []
        )
        self.calls += 1
        await asyncio.sleep(0)  # Let simultaneous requests interleave.
        last_parts = llm_request.contents[-1].parts
        response = next(
            (part.function_response for part in last_parts if part.function_response),
            None,
        )
        part = (
            types.Part(
                text=f"Found one matching user: {response.response['data']['result'][0]['userName']}."
            )
            if response
            else types.Part(
                function_call=types.FunctionCall(
                    name="search_users",
                    args={"q": last_parts[0].text.split()[-1].lower()},
                )
            )
        )
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


@pytest.fixture
def runtime(monkeypatch, stub_config, tmp_path):
    vertexai.init(
        project="customer-project",
        location="us-central1",
        credentials=AnonymousCredentials(),
    )
    monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", "123")
    monkeypatch.setenv("PING_ADMIN_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("PING_ADMIN_STATE_STORE", raising=False)
    # Track the settings changed by the real runtime set_up method.
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "customer-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setattr(agent.root_agent, "model", SearchModel())

    def search_users(q: str) -> dict:
        return {
            "status": "success",
            "data": {
                "result": [
                    {
                        "_id": f"test-user-{q}",
                        "userName": q,
                        "displayName": q.title(),
                        "mail": f"{q}@example.com",
                        "accountStatus": "active",
                    }
                ]
            },
        }

    monkeypatch.setattr(agent.root_agent, "tools", [search_users])
    runner = Runner(
        agent=agent.root_agent,
        app_name=agent.root_agent.name,
        session_service=InMemorySessionService(),
    )
    monkeypatch.setattr(a2a, "build_runner", lambda: runner)
    value = cloudpickle.loads(cloudpickle.dumps(PingAdminA2aAgent().clone()))
    value.set_up()
    return value


@pytest.mark.asyncio
async def test_runtime_card_matches_the_served_compatibility_routes(runtime):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=Starlette(routes=runtime.rest_routes)),
        base_url="http://test",
    ) as http:
        card = (await http.get("/a2a/v1/card")).json()
        assert card["protocolVersion"] == "0.3"
        assert card["preferredTransport"] == "HTTP+JSON"
        assert card["url"].endswith("/reasoningEngines/123/a2a")
        assert "localhost" not in card["url"]
        assert "supportedInterfaces" not in card
        assert card["capabilities"]["streaming"] is True
        assert card["capabilities"]["extensions"][0]["uri"] == a2ui.EXTENSION_URI
        assert card["iconUrl"] == branding.logo_url()
        # Both cards tell clients that Agent Runtime demands a Google OAuth2
        # access token with the cloud-platform scope.
        assert a2a.requires_access_token(card)
        assert card["securitySchemes"][a2a.SECURITY_SCHEME_ID]["type"] == "oauth2"
        assert card["security"] == [{a2a.SECURITY_SCHEME_ID: [a2a.CLOUD_PLATFORM_SCOPE]}]
        native = (await http.get("/a2a/card")).json()
        assert {i["url"] for i in native["supportedInterfaces"]} == {card["url"]}
        assert native["iconUrl"] == card["iconUrl"]
        assert a2a.SECURITY_SCHEME_ID in native["securitySchemes"]
    assert "on_message_send_stream" in runtime.register_operations()["a2a_extension"]
    assert isinstance(runtime, PingAdminA2aAgent)


@pytest.mark.asyncio
@pytest.mark.parametrize("enable_ui", [True, False])
async def test_runtime_search_stream_and_followup_over_real_v03_transport(
    runtime, enable_ui
):
    headers = {"X-A2A-Extensions": a2ui.EXTENSION_URI} if enable_ui else {}

    async def check_negotiation(response):
        assert response.headers.get("X-A2A-Extensions") == (
            a2ui.EXTENSION_URI if enable_ui else None
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=Starlette(routes=runtime.rest_routes)),
        headers=headers,
        base_url="http://test",
        event_hooks={"response": [check_negotiation]},
    ) as http:
        transport = CompatRestTransport(
            httpx_client=http, agent_card=None, url="http://test/a2a"
        )
        surfaces = []
        context_id = ""
        for turn in range(2):
            request = SendMessageRequest(
                message=Message(
                    message_id=f"message-{turn}",
                    context_id=context_id,
                    role=Role.ROLE_USER,
                    parts=[Part(text="Search users named Smith")],
                )
            )
            events = [
                event async for event in transport.send_message_streaming(request)
            ]
            final = events[-1].status_update
            assert final.status.state == TaskState.TASK_STATE_COMPLETED, events
            context_id = final.context_id
            messages = [message for event in events for message in ui_messages(event)]
            if enable_ui:
                # The final message contains structured data, not JSON text or a file.
                envelope = ui_messages(events[-1])
                validate_surface(envelope, BASIC_CATALOG_ID)
                assert "smith@example.com" in json.dumps(envelope)
                created = next(
                    m["createSurface"] for m in envelope if "createSurface" in m
                )
                surfaces.append(created["surfaceId"])
                assert sum("createSurface" in m for m in messages) == 2
                assert any("deleteSurface" in m for m in envelope)
            else:
                assert not messages
            assert final.status.message.parts[0].text.startswith("Found one"), events
        if enable_ui:
            assert surfaces[0] != surfaces[1]
    assert agent.root_agent.model.calls == 4


@pytest.mark.asyncio
async def test_concurrent_requests_keep_ui_negotiation_and_final_parts_separate(
    runtime,
):
    app = Starlette(routes=runtime.rest_routes)

    async def search(name, enable_ui):
        headers = {"X-A2A-Extensions": a2ui.EXTENSION_URI} if enable_ui else {}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), headers=headers
        ) as http:
            transport = CompatRestTransport(
                httpx_client=http, agent_card=None, url="http://test/a2a"
            )
            request = SendMessageRequest(
                message=Message(
                    message_id=name,
                    role=Role.ROLE_USER,
                    parts=[Part(text=f"Search users named {name}")],
                )
            )
            return [event async for event in transport.send_message_streaming(request)]

    alice, bob = await asyncio.gather(search("alice", True), search("bob", False))
    alice_ui = ui_messages(alice[-1])
    validate_surface(alice_ui, BASIC_CATALOG_ID)
    assert "alice@example.com" in json.dumps(alice_ui)
    assert "bob@example.com" not in json.dumps(alice_ui)
    assert not [message for event in bob for message in ui_messages(event)]
    assert alice[-1].status_update.context_id != bob[-1].status_update.context_id
    assert alice[-1].status_update.status.message.parts[0].text.endswith("alice.")
    assert bob[-1].status_update.status.message.parts[0].text.endswith("bob.")


def test_managed_runner_uses_customer_agent_runtime_sessions(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", "123")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "customer-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    runner = a2a.build_runner()
    assert isinstance(runner.session_service, VertexAiSessionService)
    assert runner.session_service._agent_engine_id == "123"


@pytest.mark.asyncio
async def test_managed_session_mapping_is_temporary_and_uses_runtime_assigned_ids(
    monkeypatch,
):
    # Keep the real SDK's session-ID validation and conversion. Replace only
    # its remote API so a context UUID cannot accidentally become a session ID.
    sessions = VertexAiSessionService(
        project="customer-project", location="us-central1", agent_engine_id="123"
    )
    response = SimpleNamespace(
        name="reasoningEngines/123/sessions/456",
        user_id="operator",
        update_time=datetime.now(timezone.utc),
    )

    async def no_events(**kwargs):
        for event in ():
            yield event

    api = SimpleNamespace(
        create=AsyncMock(return_value=SimpleNamespace(response=response)),
        get=AsyncMock(return_value=response),
        events=SimpleNamespace(list=AsyncMock(side_effect=no_events)),
    )

    @asynccontextmanager
    async def client():
        yield SimpleNamespace(agent_engines=SimpleNamespace(sessions=api))

    monkeypatch.setattr(sessions, "_get_api_client", client)
    runner = SimpleNamespace(app_name=agent.root_agent.name, session_service=sessions)
    runtime = AdministrationRuntime(runner, MemoryStore())
    context_id = "6f7fc873-2e6a-4b34-bc05-c8374e8e135f"
    first = await runtime.session(context_id, "operator")
    assert first.id == "456"
    api.get.assert_not_awaited()
    api.create.assert_awaited_once()
    assert (await runtime.session(context_id, "operator")).id == "456"
    api.get.assert_awaited_once_with(name="reasoningEngines/123/sessions/456")

    restarted = AdministrationRuntime(runner, MemoryStore())
    with pytest.raises(UiActionError, match="original conversation is unavailable"):
        await restarted.session(context_id, "operator", required=True)
    assert api.create.await_count == 1
    assert api.get.await_count == 1
    await restarted.session(context_id, "operator")
    assert api.create.await_count == 2
    assert api.get.await_count == 1


def test_packaging_does_not_serialize_live_executor_or_secret_values(monkeypatch):
    vertexai.init(
        project="customer-project",
        location="us-central1",
        credentials=AnonymousCredentials(),
    )
    monkeypatch.setenv("PING_ADMIN_PRIVATE_JWK", "test-secret-sentinel")
    local = PingAdminA2aAgent()
    payload = cloudpickle.dumps(local.clone())
    assert b"test-secret-sentinel" not in payload
    restored = cloudpickle.loads(payload)
    assert restored.agent_executor is None
    assert restored.request_handler is None
    assert isinstance(restored, PingAdminA2aAgent)
