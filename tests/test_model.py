"""Model migration checks through ADK and the real Gen AI request serializer."""

from __future__ import annotations

import json
import os

import httpx
import pytest
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from google.oauth2.credentials import Credentials

from ping_admin_agent import a2a, agent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_override", "location_override", "expected_model", "expected_location"),
    [
        ("", "", "gemini-3.5-flash", "global"),
        ("gemini-3.5-flash-lite", "eu", "gemini-3.5-flash-lite", "eu"),
    ],
)
async def test_vertex_endpoint_and_function_call_round_trip(
    monkeypatch,
    stub_config,
    model_override,
    location_override,
    expected_model,
    expected_location,
):
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "customer-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", "123")
    monkeypatch.setenv("PING_ADMIN_MODEL", model_override)
    monkeypatch.setenv("PING_ADMIN_MODEL_LOCATION", location_override)
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert f"/locations/{expected_location}/" in request.url.path
        assert request.url.path.endswith(f"/{expected_model}:generateContent")
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            parts = [
                {
                    "functionCall": {
                        "id": "lookup-1",
                        "name": "lookup_test_user",
                        "args": {"query": "smith"},
                    },
                    "thoughtSignature": "dGVzdC1zaWduYXR1cmU=",
                }
            ]
        else:
            history = [
                part for content in payload["contents"] for part in content["parts"]
            ]
            call = next(part for part in history if "functionCall" in part)
            response = next(
                part["functionResponse"]
                for part in history
                if "functionResponse" in part
            )
            assert call["thoughtSignature"] == "dGVzdC1zaWduYXR1cmU="
            assert response["id"] == call["functionCall"]["id"] == "lookup-1"
            assert response["name"] == call["functionCall"]["name"]
            assert response["response"] == {"match": "smith"}
            parts = [{"text": "Found the test user."}]
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"role": "model", "parts": parts},
                        "finishReason": "STOP",
                    }
                ],
            },
        )

    model = agent._build_model()
    model.client_kwargs.update(
        {
            "credentials": Credentials(token="offline-test-token"),
            "http_options": types.HttpOptions(
                async_client_args={"transport": httpx.MockTransport(respond)},
            ),
        }
    )
    monkeypatch.setattr(agent.root_agent, "model", model)

    def lookup_test_user(query: str) -> dict:
        """Look up an offline test user."""
        return {"match": query}

    monkeypatch.setattr(agent.root_agent, "tools", [lookup_test_user])
    sessions = InMemorySessionService()
    runner = Runner(
        agent=agent.root_agent, app_name=agent.root_agent.name, session_service=sessions
    )
    session = await sessions.create_session(app_name=runner.app_name, user_id="tester")
    try:
        events = [
            event
            async for event in runner.run_async(
                user_id="tester",
                session_id=session.id,
                new_message=types.Content(
                    role="user", parts=[types.Part(text="Find Smith")]
                ),
            )
        ]
        assert len(requests) == 2
        assert events[-1].content.parts[0].text == "Found the test user."
        assert os.environ["GOOGLE_CLOUD_LOCATION"] == "us-central1"
        assert a2a.build_runner().session_service._location == "us-central1"
    finally:
        await model.api_client.aio.aclose()
        model.api_client.close()
