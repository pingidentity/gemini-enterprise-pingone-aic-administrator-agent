"""Shared A2A card and ADK executor for the Agent Runtime service.

The SDK keeps A2A 1.0 protobuf objects internally. Gemini Enterprise uses the
0.3 transport adapters and card serializer; changing JSON field names alone
does not make a 1.0 server compatible with a 0.3 client.
"""

from __future__ import annotations

import os

from a2a.compat.v0_3.conversions import to_compat_agent_card
from a2a.extensions.common import get_requested_extensions
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    AuthorizationCodeOAuthFlow,
    OAuth2SecurityScheme,
    OAuthFlows,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)
from a2a.utils.constants import TransportProtocol
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService, VertexAiSessionService
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.types import Message as ASGIMessage

from . import a2ui
from .branding import logo_url
from .ui_protocol import SUPPORTED_CATALOG_IDS

DISPLAY_NAME = "PingOne AIC Administrator Agent"
DESCRIPTION = (
    "Provides user search, password reset, session/MFA management, "
    "group/role membership, direct assignments, and audit lookup."
)
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
# Agent Runtime authenticates every request at Google's API front door: a call
# without a Google OAuth2 access token carrying this scope is rejected with 401
# before the agent runs. The card declares that requirement so registrations
# and clients configure the token instead of expecting an open endpoint.
SECURITY_SCHEME_ID = "google-oauth2"
GOOGLE_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


class A2uiExtensionHeaders:
    """Acknowledge header negotiation for 0.3 clients, including SSE responses.

    A2A 1.x removed RequestContext.add_activated_extension. Its compatibility
    adapters accept old request headers but don't emit the acknowledgement
    recommended by the 0.3 extension spec. UI messages also name the extension.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        requested = get_requested_extensions(
            headers.getlist("X-A2A-Extensions") + headers.getlist("A2A-Extensions")
        )

        async def send_with_extensions(message: ASGIMessage) -> None:
            if (
                message["type"] == "http.response.start"
                and a2ui.EXTENSION_URI in requested
            ):
                response_headers = MutableHeaders(scope=message)
                response_headers["X-A2A-Extensions"] = a2ui.EXTENSION_URI
                response_headers["A2A-Extensions"] = a2ui.EXTENSION_URI
            await send(message)

        await self.app(scope, receive, send_with_extensions)


def build_agent_card(url: str) -> AgentCard:
    """Advertise the native 1.0 and Gemini-compatible 0.3 HTTP+JSON interfaces."""
    return AgentCard(
        name=DISPLAY_NAME,
        description=DESCRIPTION,
        provider=AgentProvider(
            organization="Ping Identity",
            url="https://www.pingidentity.com",
        ),
        documentation_url=(
            "https://marketplace.pingone.com/item/"
            "google-gemini-enterprise-pingone-aic-administrator-agent"
        ),
        icon_url=logo_url(),
        # Bump the patch version when branding (iconUrl, name, description) or
        # capability changes so Gemini Enterprise re-syncs its cached card
        # snapshot; identical versions are treated as unchanged.
        version="1.0.4",
        supported_interfaces=[
            AgentInterface(
                url=url,
                protocol_binding=TransportProtocol.HTTP_JSON,
                protocol_version=version,
            )
            for version in ("1.0", "0.3")
        ],
        default_input_modes=["text/plain", a2ui.MIME_TYPE],
        default_output_modes=["text/plain", a2ui.MIME_TYPE],
        capabilities=AgentCapabilities(
            streaming=True,
            extensions=[
                AgentExtension(
                    uri=a2ui.EXTENSION_URI,
                    description="Interactive AIC administration with A2UI v0.9, Material views and basic-catalog fallback.",
                    required=False,
                    params={"supportedCatalogIds": list(SUPPORTED_CATALOG_IDS)},
                )
            ],
        ),
        skills=[
            AgentSkill(
                id="pingaic_admin_agent",
                name=DISPLAY_NAME,
                description=DESCRIPTION,
                tags=["identity", "help-desk", "pingaic"],
                examples=["Search users named Smith", "Show the groups for a user"],
            )
        ],
        security_schemes={
            SECURITY_SCHEME_ID: SecurityScheme(
                oauth2_security_scheme=OAuth2SecurityScheme(
                    description=(
                        "Google OAuth2 access token with the cloud-platform scope. "
                        "Agent Runtime rejects requests without one before the "
                        "agent runs."
                    ),
                    flows=OAuthFlows(
                        authorization_code=AuthorizationCodeOAuthFlow(
                            authorization_url=GOOGLE_AUTHORIZATION_URL,
                            token_url=GOOGLE_TOKEN_URL,
                            scopes={
                                CLOUD_PLATFORM_SCOPE: "Invoke the Agent Runtime A2A service"
                            },
                        )
                    ),
                )
            )
        },
        security_requirements=[
            SecurityRequirement(
                schemes={SECURITY_SCHEME_ID: StringList(list=[CLOUD_PLATFORM_SCOPE])}
            )
        ],
    )


def requires_access_token(card: dict) -> bool:
    """True when a serialized 0.3 card demands the Google OAuth2 cloud-platform scope."""
    scheme = (card.get("securitySchemes") or {}).get(SECURITY_SCHEME_ID) or {}
    flows = scheme.get("flows") or {}
    scopes = (flows.get("authorizationCode") or {}).get("scopes") or {}
    required = any(
        CLOUD_PLATFORM_SCOPE in (item.get(SECURITY_SCHEME_ID) or [])
        for item in card.get("security") or []
        if isinstance(item, dict)
    )
    return scheme.get("type") == "oauth2" and CLOUD_PLATFORM_SCOPE in scopes and required


def gemini_agent_card(card: AgentCard) -> dict:
    """Use the SDK's 0.3 serializer, preserving extensions and transport."""
    return to_compat_agent_card(card).model_dump(by_alias=True, exclude_none=True)


def build_runner() -> Runner:
    # Import the agent at runtime, keeping resolved credentials out of the
    # deployment pickle. Secret Manager access remains lazy inside its tools.
    from .agent import root_agent

    engine_id = os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ID", "").strip()
    sessions = (
        VertexAiSessionService(
            project=os.environ["GOOGLE_CLOUD_PROJECT"],
            location=os.environ["GOOGLE_CLOUD_LOCATION"],
            agent_engine_id=engine_id,
        )
        if engine_id
        else InMemorySessionService()
    )
    return Runner(agent=root_agent, app_name=root_agent.name, session_service=sessions)


def build_executor():
    from .ui_runtime import AdministrationExecutor

    return AdministrationExecutor(build_runner)
